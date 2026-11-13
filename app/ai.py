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


async def tutor_response(message: str, level: str = "beginner", style: str = "visual") -> dict:
    client = _client()
    system = """You are LumiDesk, a patient, accurate learning coach. Explain concepts in clear, simple English without talking down to the learner. Prefer a short overview, analogy, step-by-step explanation, practical example, key takeaways and one check-for-understanding question. Do not fabricate citations or claim to have browsed. If uncertain, say what needs checking. Return ONLY valid JSON with keys: title (string), explanation (string, markdown allowed), key_points (array of strings), steps (array of objects with title and detail), analogy (string), example (string), mermaid (string; valid Mermaid flowchart or sequence diagram, no markdown fences), quiz (array of objects with question, options array, answer_index integer, explanation). Keep mermaid simple and safe. Do not include HTML or scripts."""
    response = await client.chat.completions.create(
        model=os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b"),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": f"Learner level: {level}. Preferred style: {style}.\nQuestion: {message}"}],
        temperature=0.35,
        max_tokens=1800,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"title": "Let's understand this", "explanation": raw, "key_points": [], "steps": [], "analogy": "", "example": "", "mermaid": "", "quiz": []}
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
    response = await client.chat.completions.create(
        model=os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b"),
        messages=[
            {"role": "system", "content": "You are an academic report editor. Produce original educational content and strict JSON. Respect academic integrity and never fabricate institutional approval."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}
        ],
        temperature=0.25,
        max_tokens=7000,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    if not isinstance(data.get("pages"), list):
        raise ValueError("The AI response did not contain a valid page plan.")
    return data


async def rewrite_report_pages(*, title: str, topic: str, sections: list[dict]) -> list[dict]:
    """Rewrite a long PDF in page-sized batches with bounded repair attempts."""
    import asyncio
    import re
    client = _client()
    model = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
    gate = asyncio.Semaphore(2)
    rules = (
        "You are a format-preserving academic report editor. Rewrite ONLY the "
        "selected content lines for the NEW project, not the source project. "
        "Keep the same line count, block_id values and order. Each new line must "
        "be near the original length (ideally 75%-110%, maximum 125%). "
        "Keep numbered points numbered and make consecutive lines read coherently. "
        "Never invent completed experiments, surveys, fieldwork, approvals, "
        "statistics or citations. Do not edit college names, departments, "
        "headings, photos, signatures or page numbering. If project evidence "
        "is missing, describe a proposed goal, not a completed result. "
        'Return only JSON of the form {"blocks":[{"block_id":1,"text":"example"}]}.'
    )
    async def one_page(page):
        allowed = {int(x["block_id"]): x for x in page["slots"]}
        payload = {
            "new_title": title, "new_project_details": topic,
            "section_heading_to_keep": page["section"],
            "source_body_lines_for_structure_only": [
                {"block_id": int(x["block_id"]), "target_length": x["length"],
                 "source_line": x["sample"]} for x in page["slots"]
            ],
        }
        error = "The AI did not complete this section."
        async with gate:
            for attempt in range(3):
                try:
                    response = await client.chat.completions.create(
                        model=model,
                        messages=[{"role": "system", "content": rules},
                                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                        temperature=0.25 if not attempt else 0.1,
                        max_tokens=min(4200, max(1700, len(allowed) * 140)),
                        response_format={"type": "json_object"},
                    )
                    raw = (response.choices[0].message.content or "").strip()
                    if not raw.startswith("{"):
                        raw = raw[raw.index("{"):raw.rindex("}")+1]
                    decoded = json.loads(raw)
                    if not isinstance(decoded.get("blocks"), list):
                        raise ValueError("Expected a blocks array.")
                    matched = {}
                    for entry in decoded["blocks"]:
                        key = int(entry["block_id"])
                        if key not in allowed:
                            continue
                        original = allowed[key]
                        content = " ".join(str(entry.get("text", "")).split())
                        if (len(content) < max(5, original["length"] * .35)
                                or len(content) > original["length"] * 1.35 + 9):
                            continue
                        numbered = re.match(r"^(\d+[.)])\s*", original["sample"])
                        if numbered and not content.startswith(numbered.group(1)):
                            content = numbered.group(1) + " " + content
                        matched[key] = content
                    if len(matched) != len(allowed):
                        raise ValueError(f"Only {len(matched)} of {len(allowed)} lines fit.")
                    return {"page_index": page["page_index"],
                            "blocks": [{"block_id": key, "text": matched[key]}
                                       for key in allowed]}
                except Exception as exc:
                    error = str(exc)[:200]
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
        raise AIUnavailable(
            f"Couldn't finish the {page['section']} section within the original "
            f"layout after three attempts. {error}"
        )
    return await asyncio.gather(*(one_page(page) for page in sections))
