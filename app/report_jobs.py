"""Bounded background report jobs for long-running, template-preserving PDF creation.

This is intentionally a *single-worker* free-tier implementation. Queued tasks
and output are held in process memory with a one-hour expiry, not an enterprise
job queue. A Render restart can interrupt a job. Clients receive truthful
progress and can retry from the original PDF.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import pymupdf

from .ai import rewrite_report_pages
from .db import ReportRecord, SessionLocal, record_usage
from .pdf_engine import inspect_pdf, render_replacement_pdf
from .docx_export import export_editable_docx
from .pdf_quality import verify_pdf_layout, LayoutQualityError
from .report_pipeline import build_plan, content_slots, extract_template_details

KEEP_SECONDS = 60 * 60
MAX_JOBS = 8
MAX_USER_JOBS = 4
MAX_ACTIVE_JOBS = 2
jobs: dict[str, ReportJob] = {}
active_gate = asyncio.Semaphore(MAX_ACTIVE_JOBS)


@dataclass
class ReportJob:
    id: str
    user_id: int
    source: bytes
    title: str
    topic: str
    student_name: str
    usn: str
    variant: int = 0
    mode: str = 'new_project'
    source_overlap: float | None = None
    parent_id: str | None = None
    created_at: float = field(default_factory=time.time)
    status: str = 'queued'
    detail: str = 'Your report has been queued for generation.'
    done_sections: int = 0
    total_sections: int = 0
    page_count: int = 0
    result: bytes | None = None
    word_result: bytes | None = None
    quality: dict | None = None
    ai_lines: int = 0
    fallback_lines: int = 0
    base_result: bytes | None = None  # unmodified generated PDF for reversible image edits
    image_edits: dict[str, bytes] = field(default_factory=dict)
    edit_busy: bool = False
    pages: list[dict] | None = None
    warnings: list[str] = field(default_factory=list)
    overlap: float | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)


def _prune() -> None:
    now = time.time()
    expired = [job_id for job_id, job in jobs.items()
               if job.status in ('completed', 'failed') and now - job.created_at > KEEP_SECONDS]
    for job_id in expired:
        jobs.pop(job_id, None)


def for_user(job_id: str, user_id: int) -> ReportJob | None:
    _prune()
    job = jobs.get(job_id)
    return job if job and job.user_id == user_id else None


def _comparison(a: list[dict], b: list[dict]) -> float:
    """4-word shingle Jaccard overlap of revised body sections, not plagiarism detection."""
    def text(pages):
        return ' '.join(str(x.get('text', '')) for p in pages for x in p.get('blocks', []))
    def shingles(value):
        words = re.findall(r"[\w']+", value.lower())
        return set(tuple(words[i:i+4]) for i in range(max(0, len(words)-3)))
    left, right = shingles(text(a)), shingles(text(b))
    return round(len(left & right) / max(1, len(left | right)), 4)


def create_job(*, user_id: int, source: bytes, title: str, topic: str,
               student_name: str, usn: str, parent: ReportJob | None = None, mode: str = 'new_project') -> ReportJob:
    _prune()
    user_jobs = [j for j in jobs.values() if j.user_id == user_id]
    active = [j for j in jobs.values() if j.status in ('queued', 'running')]
    if len(active) >= MAX_ACTIVE_JOBS or sum(j.status in ('queued', 'running') for j in user_jobs) >= 1:
        raise ValueError('A report is already being generated. Finish that report before starting another.')
    while len(jobs) >= MAX_JOBS or len(user_jobs) >= MAX_USER_JOBS:
        removable = sorted((j for j in jobs.values() if j.status not in ('queued', 'running')),
                           key=lambda j: j.created_at)
        if not removable:
            raise ValueError('The report processor is at capacity; please try again shortly.')
        old = removable[0]
        jobs.pop(old.id, None)
        user_jobs = [j for j in jobs.values() if j.user_id == user_id]
    job = ReportJob(
        id=secrets.token_urlsafe(18), user_id=user_id, source=source,
        title=title, topic=topic, student_name=student_name, usn=usn,
        parent_id=parent.id if parent else None, variant=(parent.variant + 1) if parent else 0,
        mode=parent.mode if parent else mode,
    )
    jobs[job.id] = job
    job.task = asyncio.create_task(run_job(job))
    return job


def public_job(job: ReportJob) -> dict[str, Any]:
    return {
        'job_id': job.id, 'status': job.status, 'detail': job.detail, 'mode': job.mode,
        'source_overlap_estimate': job.source_overlap,
        'done_sections': job.done_sections, 'total_sections': job.total_sections,
        'pages': job.page_count, 'warnings': job.warnings[:14],
        'quality': {k:v for k,v in job.quality.items() if k != 'page_checks'} if job.quality else None,
        'ai_lines': job.ai_lines, 'fallback_lines': job.fallback_lines,
        'overlap_estimate': job.overlap,
        'download_url': f'/api/reports/jobs/{job.id}/download?format=pdf' if job.status == 'completed' else None,
        'downloads': {
            'pdf': f'/api/reports/jobs/{job.id}/download?format=pdf' if job.status == 'completed' and job.result else None,
            'docx': f'/api/reports/jobs/{job.id}/download?format=docx' if job.status == 'completed' and job.word_result else None,
        },
        'regenerate_url': f'/api/reports/jobs/{job.id}/regenerate' if job.status == 'completed' else None,
        'image_edit_url': f'/api/reports/jobs/{job.id}/images' if job.status == 'completed' and job.result else None,
        'image_edit_count': len(job.image_edits),
        'image_edit_busy': job.edit_busy,
        'error': job.error,
        'temporary': True,
    }


async def run_job(job: ReportJob) -> None:
    async with active_gate:
        job.status = 'running'
        job.detail = 'Reading the original PDF and mapping its sections…'
        try:
            # PDF operations are CPU-heavy; keep the web server responsive to polls.
            def inspect():
                document, template = inspect_pdf(job.source)
                document.close()
                return template
            template = await asyncio.to_thread(inspect)
            analysis = extract_template_details(template)
            sections = content_slots(template)
            job.total_sections = len(sections)
            job.page_count = template['page_count']
            parent = jobs.get(job.parent_id) if job.parent_id else None
            if parent and parent.user_id != job.user_id:
                parent = None
            previous = parent.pages if parent else None

            def update_progress(done: int, total: int) -> None:
                job.done_sections = done
                job.total_sections = total
                job.detail = f'Writing report sections: {done} of {total} processed…'

            if sections:
                generated = await rewrite_report_pages(
                    title=job.title, topic=job.topic, sections=sections,
                    variant_seed=job.variant, previous_pages=previous, progress=update_progress,
                )
                job.warnings.extend(w for p in generated for w in p.get('warnings', []))
                fallbacks = sum(int(p.get('fallback_count', 0)) for p in generated)
                if fallbacks:
                    job.warnings.append(
                        f'{fallbacks} text lines used a topic-based draft fallback after missing or invalid AI output. Review these sections carefully.'
                    )
                if previous:
                    job.overlap = _comparison(generated, previous)
                    if job.overlap > .30:
                        job.detail = 'Checking a more distinct writing variant…'
                        alternate = await rewrite_report_pages(
                            title=job.title, topic=job.topic, sections=sections,
                            variant_seed=job.variant + 7, previous_pages=previous,
                            progress=update_progress,
                        )
                        alternative_overlap = _comparison(alternate, previous)
                        if alternative_overlap < job.overlap:
                            generated = alternate
                            job.overlap = alternative_overlap
                            job.warnings.extend(w for p in alternate for w in p.get('warnings', []))
                            fallbacks = sum(int(p.get('fallback_count', 0)) for p in alternate)
                            if fallbacks:
                                job.warnings.append(f'{fallbacks} replacement lines need review because they used draft fallback wording.')
                    if job.overlap > .30:
                        job.warnings.append(
                            'Measured body-text 4-word-shingle overlap exceeds the requested 30% target. '
                            'This estimate is not a plagiarism or originality certificate.'
                        )
            else:
                generated = []
                job.warnings.append('No safe body-text regions were detected; only identified title and student details were changed.')
            if job.mode == 'approved_reference' and generated:
                source_blocks = [
                    {'page_index': page['page_index'],
                     'blocks': [{'block_id': row['block_id'], 'text': row['sample']} for row in page['slots']]}
                    for page in sections
                ]
                job.source_overlap = _comparison(generated, source_blocks)
                if job.source_overlap > 0.30:
                    job.detail = 'Revising overly similar body wording…'
                    alternate = await rewrite_report_pages(
                        title=job.title, topic=job.topic, sections=sections,
                        variant_seed=job.variant + 9, previous_pages=source_blocks,
                        progress=update_progress,
                    )
                    revised = _comparison(alternate, source_blocks)
                    if revised < job.source_overlap:
                        generated = alternate
                        job.source_overlap = revised
                    if job.source_overlap > .30:
                        job.warnings.append('Estimated original-reference overlap exceeds 30%. This is not a plagiarism score; review and write from your own evidence.')
                job.warnings.append('A reference file is not evidence of your own work. Replace photos and verify all citations, dates, outcomes and faculty approval.')
            job.fallback_lines = sum(int(p.get('fallback_count', 0)) for p in generated)
            job.ai_lines = max(0, sum(len(p['slots']) for p in sections) - job.fallback_lines)
            if sections and not job.ai_lines:
                job.warnings.insert(0, 'AI MODEL DID NOT PROVIDE CONTENT. This is an offline draft with placeholder-style prose; review and rewrite before submission.')
            job.detail = 'Fitting replacement text into the existing PDF layout…'
            plan = build_plan(template, analysis, job.title, job.student_name, job.usn, generated,
                              preserve_references=True)
            job.warnings.append('Original references remain visually in place. Verify each source against your actual work before submission.')
            if plan['rewritten_body_slots'] != plan['planned_body_slots'] or plan['rejected_ai_slots']:
                # Do not pass through incomplete model output as if it were verified.
                job.warnings.append('Some text areas needed review after the layout-fitting check.')
            def render():
                return render_replacement_pdf(job.source, template, plan)
            result, warnings = await asyncio.to_thread(render)
            # Verify the final file, rather than treating a model response as a PDF.
            with pymupdf.open(stream=result, filetype='pdf') as final:
                if len(final) != template['page_count']:
                    raise ValueError('The reconstructed document had a different page count.')
                if final.is_encrypted or not result.startswith(b'%PDF-'):
                    raise ValueError('The reconstructed file was not a valid readable PDF.')
            job.detail = 'Visually comparing every generated PDF page against your uploaded template…'
            job.quality = await asyncio.to_thread(verify_pdf_layout, job.source, result, template, plan)
            # Generate DOCX from the exact completed PDF so both files contain
            # the same body text, images and page ordering.
            job.detail = 'Building editable Word textboxes with the measured font positions…'
            try:
                job.word_result, word_warnings = await asyncio.to_thread(export_editable_docx, result)
                job.warnings.extend(word_warnings[:8])
            except Exception as exc:
                logging.exception('Word export failed for report %s', job.id)
                job.word_result = None
                job.warnings.append('PDF ready, but Word export could not be completed for this template. Please download the PDF and review it.')
            job.warnings.extend(analysis.get('warnings', []))
            job.warnings.extend(warnings)
            job.warnings.append('Review facts and verify any original photographs, certificates, signatures and references before submission.')
            job.result = result
            job.pages = generated
            job.done_sections = job.total_sections
            job.detail = ('PDF and Word ready. Visual layout comparison passed on every page.' if job.word_result
                          else 'PDF ready. Visual layout comparison passed; Word needs review.')
            job.status = 'completed'
            try:
                with SessionLocal() as db:
                    db.add(ReportRecord(user_id=job.user_id, title=job.title,
                                        pages=template['page_count'], status='completed'))
                    record_usage(db, job.user_id, 'report', job.title[:240])
                    db.commit()
            except Exception:
                logging.exception('Report complete, but analytics database could not be updated')
                job.warnings.append('Analytics could not be saved; the downloadable PDF is unaffected.')
        except asyncio.CancelledError:
            job.status = 'failed'
            job.error = 'The server interrupted generation. Please restart using the source PDF.'
            raise
        except LayoutQualityError as exc:
            logging.warning('Visual QA blocked report %s: %s', job.id, exc)
            job.status = 'failed'
            job.error = str(exc)[:350] + ' The PDF was not released because its template layout changed.'
            job.detail = 'Visual comparison found a layout mismatch.'
        except Exception as exc:
            logging.exception('Background report generation failed: %s', job.id)
            job.status = 'failed'
            job.error = ('PDF could not be safely processed. Ensure it is unlocked, has selectable text, '
                         'and fits within 70 pages / the upload size limit.')
            job.detail = f'Generation stopped: {type(exc).__name__}'
