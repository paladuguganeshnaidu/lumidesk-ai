from __future__ import annotations
import json
import os


class AIUnavailable(RuntimeError):
    pass


def _client():
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise AIUnavailable("The AI client package is not installed. Redeploy after installing requirements.txt.") from exc
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not key:
        raise AIUnavailable("AI is not configured yet. Add NVIDIA_API_KEY in the Render environment settings.")
    return AsyncOpenAI(api_key=key, base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"), timeout=100.0, max_retries=2)


async def _completion(client, *, messages: list[dict], max_tokens: int, temperature: float):
    """NVIDIA Nemotron direct answer; retry models that reject NIM options."""
    import logging
    model = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
    payload = dict(model=model, messages=messages, temperature=temperature,
                   max_tokens=max_tokens,
                   extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    try:
        return await client.chat.completions.create(**payload)
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        if code in (400, 422):
            payload.pop("extra_body", None)
            try:
                return await client.chat.completions.create(**payload)
            except Exception as retry_error:
                exc = retry_error
                code = getattr(exc, "status_code", None)
        logging.warning("NVIDIA inference failed: status=%s type=%s model=%s", code, type(exc).__name__, model[:100])
        if code == 401:
            detail = "NVIDIA rejected the configured API key. Check NVIDIA_API_KEY in Render."
        elif code == 403:
            detail = "The NVIDIA API key has no access to the selected model."
        elif code == 404:
            detail = "The configured NVIDIA_MODEL was not found. Check the model ID."
        elif code == 429:
            detail = "The NVIDIA API quota or rate limit was reached. Retry later."
        elif code in (400, 422):
            detail = "The NVIDIA model rejected its request format. Review NVIDIA_MODEL."
        elif code and code >= 500:
            detail = "NVIDIA is temporarily unavailable. Retry later."
        else:
            detail = "Unable to reach the NVIDIA model. Check endpoint and connection."
        raise AIUnavailable(detail) from exc


def _json_object(raw: str) -> dict | None:
    import re
    raw = (raw or "").strip()
    fence = chr(96) * 3
    if raw.startswith(fence):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
        raw = raw.rsplit(fence, 1)[0].strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(raw[start:end + 1])
                return obj if isinstance(obj, dict) else None
            except (json.JSONDecodeError, ValueError):
                pass
    return None


async def tutor_response(message: str, level: str = "beginner", style: str = "visual") -> dict:
    client = _client()
    system = """You are LumiDesk, a patient, accurate learning coach. Explain concepts in clear, simple English without talking down to the learner. Prefer a short overview, analogy, step-by-step explanation, practical example, key takeaways and one check-for-understanding question. Do not fabricate citations or claim to have browsed. If uncertain, say what needs checking. Return ONLY valid JSON with keys: title (string), explanation (string, markdown allowed), key_points (array of strings), steps (array of objects with title and detail), analogy (string), example (string), mermaid (string; valid Mermaid flowchart or sequence diagram, no markdown fences), quiz (array of objects with question, options array, answer_index integer, explanation). Keep mermaid simple and safe. Do not include HTML or scripts."""
    response = await _completion(client, messages=[
        {"role": "system", "content": system},
        {"role": "user", "content": f"Learner level: {level}. Preferred style: {style}.\nQuestion: {message}"},
    ], temperature=0.35, max_tokens=3200)
    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        raise AIUnavailable("NVIDIA returned no final answer. Try again or check the model configuration.")
    data = _json_object(raw)
    if data is None:
        data = {"title": "Learning explanation", "explanation": raw[:10000],
                "key_points": [], "steps": [], "analogy": "", "example": "", "mermaid": "", "quiz": []}
    data.setdefault("title", "Let's understand this")
    data.setdefault("explanation", "")
    for key in ("key_points", "steps", "quiz"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data["mermaid"] = str(data.get("mermaid", ""))[:5000]
    return data


async def report_plan(*, topic: str, title: str, student: str, institution: str, department: str, guide: str, instructions: str, template: dict) -> dict:
    client = _client()
    # Only structural cues and short block labels are supplied to the model.
    # The source report's substantive paragraphs are deliberately not reused.
    prompt = {
        "new_project": {"topic": topic, "title": title, "student": student, "institution": institution, "department": department, "guide": guide, "instructions": instructions},
        "template_structure": template,
        "rules": [
            "Write original, topic-specific academic project report content; do not copy or paraphrase source-report sentences.",
            "Preserve the reference's page order and approximate text lengths per slot.",
            "Keep headings only when they are generic structural headings; adapt topic-specific headings to the new project.",
            "Never invent signatures, seals, grades, approvals, supervisor endorsements, survey results, experiments, datasets, or references.",
            "For certificate/declaration/approval/signature slots, use a neutral placeholder such as 'To be completed and signed by the authorized faculty member'.",
            "Use plausible report sections only where the source layout indicates them. Mark unknown project-specific facts as [STUDENT TO VERIFY].",
            "Return JSON only: {pages:[{page_index:int,blocks:[{block_id:int,text:string}]}]}.",
            "Every provided block_id must appear exactly once. Keep each text block concise enough to fit its original rectangle. Use the structural_label only as a layout cue; do not reproduce any original body text."
        ]
    }
    response = await _completion(client, messages=[
        {"role": "system", "content": "You are an academic report editor. Produce original educational content and strict JSON. Never fabricate approval."},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ], temperature=0.25, max_tokens=7000)
    data = _json_object(response.choices[0].message.content or "") or {}
    if not isinstance(data.get("pages"), list):
        raise ValueError("The AI response did not contain a valid page plan.")
    return data


async def rewrite_report_pages(*, title: str, topic: str, sections: list[dict],
                               variant_seed: int = 0, previous_pages: list[dict] | None = None,
                               progress=None) -> list[dict]:
    """Generate bounded line-sized prose, with non-failing draft fallbacks.

    Each provider call covers no more than 10 lines. Provider errors, truncated
    JSON and missing block IDs are handled by filling the affected slots with
    topic-relevant *draft* text rather than aborting the entire PDF. Callers
    must disclose all fallbacks to the student. Runtime depends on provider
    quotas; this function cannot promise a completed AI-assisted answer.
    """
    import asyncio
    import hashlib
    import logging
    import re

    gate = asyncio.Semaphore(2)
    total = len(sections)
    completed = 0
    try:
        client = _client()
        client_error = None
    except (AIUnavailable, Exception) as exc:
        client = None
        client_error = f"AI unavailable: {type(exc).__name__}"

    prev_map = {}
    for page in previous_pages or []:
        prev_map[int(page.get('page_index', -1))] = {
            int(b['block_id']): str(b.get('text', '')) for b in page.get('blocks', [])
            if isinstance(b, dict) and 'block_id' in b
        }

    def words(text: str):
        return re.findall(r"[\w']+", text.lower())

    def overlap(a: str, b: str) -> float:
        a_words, b_words = words(a), words(b)
        n = 4 if min(len(a_words), len(b_words)) >= 4 else 2
        aa = set(zip(*(a_words[i:] for i in range(n))))
        bb = set(zip(*(b_words[i:] for i in range(n))))
        return len(aa & bb) / max(1, len(aa | bb))

    def fit(text: str, slot: dict, page_index: int, row: int) -> str:
        original = str(slot.get('sample', ''))
        target = max(12, int(slot.get('length', len(original)) or 12))
        cleaned = ' '.join(str(text or '').split()).strip()
        prefix = re.match(r'^(\d+[.)])\s*', original)
        if prefix and not cleaned.startswith(prefix.group(1)):
            cleaned = f'{prefix.group(1)} {cleaned}'
        # Preserve line geometry; never send new text outside its bounding box.
        max_len = target + min(9, max(2, int(target * .10)))
        if len(cleaned) > max_len:
            cut = cleaned[:max_len + 1].rsplit(' ', 1)[0]
            cleaned = cut if cut and len(cut) >= min(12, target * .45) else cleaned[:max_len]
        return cleaned.strip()

    topic_line = re.sub(r'\s+', ' ', topic).strip(' .')
    title_line = re.sub(r'\s+', ' ', title).strip(' .')
    anchors = [x.strip() for x in re.split(r'(?<=[.!?])\s+|[;\n]+', topic_line) if x.strip()]
    anchors = anchors[:8] or [topic_line]
    # Deliberately use proposed/future tense: a title is not proof that an
    # experiment, an NSS activity or an institutional assessment took place.
    openers = [
        'The proposed study examines', 'This project considers', 'The report outlines',
        'The planned work investigates', 'One design objective concerns',
        'The approach explores', 'A key discussion point is', 'The project aims to clarify',
        'The study plan addresses', 'The framework focuses on', 'This analysis describes',
        'The expected outcome involves', 'An important consideration is',
        'The project scope includes', 'The implementation plan discusses',
        'The proposed method evaluates', 'The team intends to explore',
        'This section introduces', 'The report reviews', 'Further work should verify'
    ]
    objects = [
        'its practical purpose', 'the main design challenge', 'real-world constraints',
        'the expected learning outcomes', 'a clear implementation sequence',
        'the proposed workflow', 'responsible use and limitations',
        'the key technical decisions', 'the relevance of the identified problem',
        'the design and testing approach', 'suitable evaluation criteria',
        'how the solution could be refined', 'the materials and resources needed',
        'ways to verify the eventual results', 'the intended users and their needs',
        'the next steps for implementation', 'the relationship between goals and methods'
    ]
    section_detail = {
        'abstract': ['overview', 'motivation', 'intended contribution', 'planned outcomes'],
        'introduction': ['background', 'need for the study', 'proposed scope', 'problem context'],
        'objectives': ['learning goal', 'measurable objective', 'planned deliverable', 'research direction'],
        'methodology': ['planned procedure', 'design stage', 'evaluation plan', 'required resources'],
        'results': ['expected observations', 'proposed validation', 'future testing', 'limits of available evidence'],
        'conclusion': ['intended outcomes', 'remaining work', 'practical considerations', 'further validation'],
    }

    def fallback(slot: dict, section: str, page_index: int, row: int) -> str:
        seed_source = f'{title_line}|{topic_line}|{variant_seed}|{section}|{page_index}|{row}'
        k = int.from_bytes(hashlib.sha256(seed_source.encode()).digest()[:4], 'big')
        opener = openers[(k + row + variant_seed * 3) % len(openers)]
        obj = objects[(k // 17 + page_index + variant_seed * 7) % len(objects)]
        focus = (section_detail.get(section.lower(), ['project rationale', 'context', 'design detail', 'evaluation plan'])
                 [(k // 11 + row) % 4])
        anchor = anchors[(k // 9 + row + variant_seed) % len(anchors)]
        # Long anchored sentences are fitted into the original line length.
        # For short lines, emit a concise, editable academic draft fragment.
        target = int(slot.get('length', 70))
        if target < 38:
            line = f'{focus.capitalize()}: {obj}.' if not variant_seed else f'{obj.capitalize()} and {focus}.'
        elif target < 68:
            line = (f'{opener} {focus} and {obj}.' if not variant_seed else
                    f'{obj.capitalize()} forms part of the {focus} plan.')
        else:
            shortened = anchor[:min(len(anchor), max(24, target // 2))].rstrip(' ,.;:')
            if not variant_seed:
                line = f'{opener} {focus} for {title_line}. It relates to {shortened} and {obj}.'
            elif variant_seed % 3 == 1:
                line = f'{obj.capitalize()} shapes {focus}; practical decisions are guided by {shortened}.'
            elif variant_seed % 3 == 2:
                line = f'For {focus}, consider {obj} alongside the topic: {shortened}.'
            else:
                line = f'Examining {shortened} gives a basis to discuss {obj} while refining {focus}.'
        return fit(line, slot, page_index, row)


    def fallback_page(page: dict) -> dict[int, str]:
        """Readable planned-content prose distributed across unchanged line slots."""
        section = page['section']
        slots = page['slots']
        subject = title_line[:85]
        concept = anchors[0][:140].rstrip(' ,.;:')
        # This is explicitly a proposal/draft, never an account of completed
        # fieldwork, completed experiments or an authorized approval.
        if variant_seed % 2 == 0:
            sentences = [
                f'{subject} is proposed to address {concept.lower()}.',
                f'The {section.lower()} sets out the rationale and intended scope of this work.',
                'The underlying challenge is considered from the perspective of practical student use.',
                'The proposal identifies clear goals and a structured sequence for reaching them.',
                'Each stage should connect the core idea with a specific design choice and review point.',
                'The planned workflow begins with requirements, moves through implementation and ends with validation.',
                'Possible limitations include resource availability, data quality and user expectations.',
                'No experiments or community activities are presented as completed without supporting evidence.',
                'An appropriate evaluation should examine usability, correctness and consistency of the intended result.',
                'The report outlines the anticipated contribution without inventing measurements or external findings.',
                'Implementation choices will be documented with attention to reliability and responsible operation.',
                'Feedback from relevant participants can guide later refinement once the project has been tested.',
                'A clear review of outcomes will distinguish proposed functionality from verified performance.',
                'Where reliable facts are not yet available, the completed work must be checked by the student.',
                'The expected deliverable is a documented approach that can be reviewed and improved.',
                'Future work may examine wider deployment, alternate methods and additional validation needs.',
            ]
        else:
            sentences = [
                f'An investigation into {concept.lower()} motivates the design of {subject}.',
                f'This {section.lower()} describes the central question before outlining practical responses.',
                'The discussion begins with intended users, operating conditions and relevant constraints.',
                'A staged design approach offers a basis for comparing alternatives and managing uncertainty.',
                'During planning, every proposed function should be linked to a documented requirement.',
                'The intended system can then be developed in manageable components with observable checks.',
                'Accuracy and usefulness require testing against clearly defined assessment criteria.',
                'The student should document observed evidence and distinguish it from expected behavior.',
                'Changes to the proposed solution may be guided by structured feedback and review.',
                'Operational risks, usability concerns and maintenance needs deserve explicit consideration.',
                'This document provides a basis for subsequent implementation rather than a claim of completion.',
                'Evaluation results should be recorded only after the relevant activities have actually occurred.',
                'A further iteration could explore expanded capabilities once the baseline is verified.',
                'The proposed contribution is framed as a testable objective, not an assumed achievement.',
                'Conclusions must reflect actual outcomes and the limitations of any supporting evidence.',
                'The work therefore proceeds through reasoning, practical development and critical verification.',
            ]
        # Across longer reports, start from a different point in the narrative
        # to avoid duplicate paragraphs on repeated pages of the same section.
        offset = (page['page_index'] * 7 + variant_seed * 5) % len(sentences)
        rotated = sentences[offset:] + sentences[:offset]
        words = []
        needed = sum(max(14, int(s['length'])) for s in slots)
        while len(' '.join(words)) < needed + 150:
            words.extend(' '.join(rotated).split())
            # Alternate sentence order during successive passes.
            rotated = rotated[4:] + rotated[:4]
        out = {}
        position = 0
        for i, slot in enumerate(slots):
            budget = max(12, int(slot['length']) - 3)
            line = []
            while position < len(words):
                candidate = ' '.join(line + [words[position]])
                if line and len(candidate) > budget:
                    break
                if len(candidate) > budget + 6:
                    break
                line.append(words[position]); position += 1
            if not line and position < len(words):
                line.append(words[position]); position += 1
            raw = ' '.join(line)
            out[int(slot['block_id'])] = fit(raw, slot, page['page_index'], i)
        return out

    def decode_blocks(raw: str) -> list[dict]:
        raw = raw.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.I)
            raw = re.sub(r'\s*```\s*$', '', raw)
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            first, last = raw.find('{'), raw.rfind('}')
            if first < 0 or last < first:
                return []
            try:
                value = json.loads(raw[first:last+1])
            except (json.JSONDecodeError, ValueError):
                return []
        if not isinstance(value, dict):
            return []
        return value.get('blocks', []) if isinstance(value.get('blocks'), list) else []

    async def one_page(page: dict) -> dict:
        nonlocal completed
        slots = page['slots']
        expected = {int(s['block_id']): (i, s) for i, s in enumerate(slots)}
        result = {}
        fallback_count = 0
        notes = []
        if client is not None:
            for start in range(0, len(slots), 10):
                chunk = slots[start:start+10]
                payload = {
                    'project_title': title_line, 'project_description': topic_line,
                    'section_heading_to_preserve': page['section'],
                    'writing_variant': variant_seed,
                    'write_original_academic_draft': True,
                    'slots': [{
                        'block_id': int(s['block_id']), 'target_characters': int(s['length']),
                        'source_line_for_context_only': str(s['sample'])[:190],
                        'previous_variant_to_avoid': prev_map.get(page['page_index'], {}).get(int(s['block_id']), '')[:190]
                    } for s in chunk]
                }
                prompt = (
                    'Write ORIGINAL, project-specific and coherent academic BODY prose for the requested concept, '
                    'not a paraphrase of the uploaded student work. '
                    'The text in adjacent slots MUST read as a continuous narrative, not unrelated standalone sentences. '
                    'Keep each line within about 95 to 105 percent of its target character count and preserve '
                    'paragraph endings and list-number prefixes. The layout fitter preserves the exact original '
                    'font size, baseline, margin and number of lines; make the wording fit these slots naturally. '
                    'Do not alter headings, institution details, page footers, signatures, certificates or photographs. '
                    'Never fabricate completed fieldwork, survey results, approvals, references or measurements. '
                    'For another variant use distinct substance and sentence structure without reusing previous wording. '
                    'Return valid JSON only: {"blocks":[{"block_id":int,"text":str}]}.'
                )
                batch = {}
                async with gate:
                    for attempt in range(2):
                        try:
                            answer = await asyncio.wait_for(_completion(
                                client, messages=[{'role': 'system', 'content': prompt},
                                          {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                                temperature=min(.8, .32 + variant_seed * .13 + attempt * .06),
                                max_tokens=2400
                            ), timeout=75)
                            raw = (answer.choices[0].message.content or '').strip()
                            for item in decode_blocks(raw):
                                try:
                                    bid = int(item.get('block_id', -1))
                                    if bid not in expected:
                                        continue
                                    i, slot = expected[bid]
                                    text = fit(str(item.get('text', '')), slot, page['page_index'], i)
                                    if text and len(text) >= min(8, int(slot['length']) // 3):
                                        if overlap(text, slot['sample']) >= .78:
                                            continue
                                        previous = prev_map.get(page['page_index'], {}).get(bid)
                                        if previous and overlap(text, previous) >= .45:
                                            continue
                                        batch[bid] = text
                                except (TypeError, ValueError, AttributeError):
                                    continue
                            if len(batch) == len(chunk):
                                break
                            if attempt == 0:
                                payload['slots'] = [row for row in payload['slots'] if row['block_id'] not in batch]
                                if not payload['slots']:
                                    break
                        except Exception as exc:
                            if attempt == 1:
                                notes.append(f'AI provider gave an invalid/incomplete reply ({type(exc).__name__})')
                            else:
                                await asyncio.sleep(.7)
                result.update(batch)
        elif client_error:
            notes.append(client_error)
        offline_lines = fallback_page(page) if not result else {}
        for row, slot in enumerate(slots):
            bid = int(slot['block_id'])
            if bid not in result:
                result[bid] = offline_lines.get(bid) or fallback(slot, page['section'], page['page_index'], row)
                fallback_count += 1
        completed += 1
        if progress is not None:
            try:
                progress(completed, total)
            except Exception:
                logging.exception('Progress callback failed')
        return {
            'page_index': page['page_index'],
            'blocks': [{'block_id': bid, 'text': result[bid]} for bid in expected],
            'fallback_count': fallback_count,
            'warnings': notes[:3],
        }

    return await asyncio.gather(*(one_page(page) for page in sections))
