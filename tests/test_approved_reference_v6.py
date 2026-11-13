"""Separate consent-gated, topic-preserving approved-reference workflow."""
import asyncio
import time
from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pdf_engine import inspect_pdf
from app.report_pipeline import extract_approved_reference_context, extract_template_details, content_slots
from app.report_jobs import jobs

SAMPLE = Path('/mnt/data/NANDHUSHREE_NSS.pdf')


def sample_pdf() -> bytes:
    if SAMPLE.exists():
        return SAMPLE.read_bytes()
    # Reproducible synthetic CI fixture when the private user upload is unavailable.
    doc = fitz.open()
    for i in range(14):
        page = doc.new_page(width=595,height=842)
        if i == 0:
            page.insert_text((65,65),'NAGARJUNA COLLEGE OF ENGINEERING AND TECHNOLOGY', fontsize=13)
            page.insert_text((65,180),'Report Titled',fontsize=13)
            page.insert_text((65,205),'Cyber Suraksha Abhiyan',fontsize=14)
            page.insert_text((65,240),'Nandhushree M(1NC24CS133)',fontsize=12)
        elif i==4:
            page.insert_text((60,90),'Abstract',fontsize=15)
        elif i==6:
            page.insert_text((60,90),'Introduction',fontsize=15)
        elif i==7:
            page.insert_text((60,90),'Objectives',fontsize=15)
        elif i==9:
            page.insert_text((60,90),'Methodology',fontsize=15)
        elif i==13:
            page.insert_text((60,90),'References',fontsize=15)
        for row in range(5):
            if i in (4,6,7,9):
                page.insert_text((65,160+row*24), f'This project examines cyber safety, phishing risk, passwords and digital security {row}', fontsize=10)
    result=doc.tobytes(deflate=True)
    doc.close()
    return result



def test_reference_reads_original_topic_and_ignores_student_identity():
    doc, template = inspect_pdf(sample_pdf())
    doc.close()
    old = extract_template_details(template)
    title, topic = extract_approved_reference_context(template, old)
    assert 'Cyber Suraksha Abhiyan' in title
    assert 'Nandhushree' not in topic
    assert '1NC24CS133' not in topic
    assert 'cyber' in topic.lower()
    assert len(content_slots(template)) > 3


def test_separate_consent_and_result_with_14_page_pdf(monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'current_user', lambda req: {'id': 9033, 'active': True})
    monkeypatch.setattr(main, 'require_csrf', lambda req: None)
    monkeypatch.setattr('app.ai._client', lambda: (_ for _ in ()).throw(RuntimeError('provider not available')))
    monkeypatch.setattr('app.report_jobs.record_usage', lambda db,*args,**kw: None)
    monkeypatch.setattr('app.report_jobs.SessionLocal', lambda: (_ for _ in ()).throw(RuntimeError('analytics disabled in unit test')))
    jobs.clear()
    payload = sample_pdf()
    with TestClient(app) as client:
        def start(confirm):
            return client.post('/api/reports/approved/start', files={'file':('approved.pdf',payload,'application/pdf')}, data={
                'student_name':'Test Student', 'usn':'1NC24CS777', 'permission_granted':confirm,
            })
        refused = start('false')
        assert refused.status_code == 422, refused.text
        ok = start('true')
        assert ok.status_code == 202, ok.text
        jid=ok.json()['job_id']; assert ok.json()['mode']=='approved_reference'
        for _ in range(400):
            poll=client.get('/api/reports/jobs/'+jid)
            if poll.json()['status'] in ('completed','failed'): break
            time.sleep(.025)
        assert poll.json()['status']=='completed',poll.text
        meta=poll.json()
        assert meta['source_overlap_estimate'] is not None
        assert meta['downloads']['pdf']
        pdf=client.get(meta['downloads']['pdf'])
        assert pdf.status_code==200
        with fitz.open(stream=pdf.content,filetype='pdf') as output:
            assert len(output)==14
            assert 'Test Student' in output[0].get_text().replace('\u00a0',' ')
            assert '1NC24CS777' in output[0].get_text()
            assert 'Cyber Suraksha Abhiyan' in output[0].get_text()
            assert 'Nandhushree' not in output[0].get_text()
            if SAMPLE.exists():
                assert output[10].get_images(full=True)
            assert 'References' in output[13].get_text()
        # Verify a second draft is produced from the same original PDF.
        regenerate=client.post(f'/api/reports/jobs/{jid}/regenerate', data={'student_name':'Another Person','usn':'1NC24CS888'})
        assert regenerate.status_code==202,regenerate.text
        next_id=regenerate.json()['job_id']
        for _ in range(400):
            poll=client.get('/api/reports/jobs/'+next_id)
            if poll.json()['status'] in ('completed','failed'): break
            time.sleep(.025)
        assert poll.json()['status']=='completed',poll.text
        with fitz.open(stream=client.get(poll.json()['downloads']['pdf']).content,filetype='pdf') as new_doc:
            assert 'Another Person' in new_doc[0].get_text().replace('\u00a0',' ')
            assert len(new_doc)==14


def test_frontend_has_separate_tab_download_fix_and_focus_timer():
    js=Path('app/static/app.js').read_text()
    html=Path('app/templates/workspace.html').read_text()
    assert "downloadFormat('pdf')" in js
    assert 'if (!lastDownloadUrl)' not in js
    assert 'id="approved-form"' in html
    assert 'id="focus-panel"' in html
    assert "'/api/reports/approved/start'" in js
