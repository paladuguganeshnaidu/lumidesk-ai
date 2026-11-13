"""Template-first report editing: preserve headings, institution, tables and pictures."""
from __future__ import annotations
import re

USN_RE = re.compile(r"\b[0-9][A-Z]{2}[0-9]{2}[A-Z]{2}[0-9]{3}\b", re.I)
BODY = {
    "abstract": "Abstract", "introduction": "Introduction",
    "objectives": "Objectives", "description of the activity": "Description of the Activity",
    "description of activity": "Description of the Activity",
    "methodology": "Methodology", "implementation": "Implementation",
    "literature review": "Literature Review", "system design": "System Design",
    "results": "Results", "discussion": "Discussion", "conclusion": "Conclusion",
    "future work": "Future Work", "problem statement": "Problem Statement",
}
PROTECTED = ("table of contents", "certificate", "acknowledgement", "acknowledgment",
             "declaration", "photographs", "annexure", "references", "bibliography")
LOCKED = ("department of", "dept of", "college of", "university", "coordinator",
          "principal", "prof.", "dr.", "signature", "examiner", "approved",
          "academic year", "submitted by", "national service scheme")


def section_of(page: dict) -> str:
    # Exact standalone headings: avoid classifying TOC as certificate or body as photographs.
    labels = [re.sub(r"\s+", " ", b["text"].strip().lower()).strip(" .:-")
              for b in page["blocks"] if len(b["text"].strip()) <= 48]
    for label in PROTECTED:
        if label in labels:
            return label
    for label, title in BODY.items():
        if label in labels:
            return title
    return ""


def extract_template_details(template: dict) -> dict:
    pages = template.get("pages", [])
    cover = "\n".join(b["text"] for b in pages[0]["blocks"]) if pages else ""
    hit = re.search(r"report\s+titled\s*[\"'“‘]?\s*([^\"'”’\n]{5,110})", cover, re.I)
    old_title = hit.group(1).strip(" .“”'") if hit else ""
    if not old_title:
        quotes = re.findall(r'[“"]([^”"]{9,110})[”"]', cover)
        old_title = next((v for v in quotes if "grade" not in v.lower()), "")
    hit = USN_RE.search(cover)
    old_usn = hit.group(0) if hit else ""
    old_name = ""
    if old_usn:
        line = next((v for v in cover.splitlines() if old_usn.lower() in v.lower()), "")
        old_name = re.sub(r"[^\w .-]", "", re.sub(r"\(?\s*" + re.escape(old_usn) + r"\s*\)?",
                            "", line, flags=re.I)).strip()[:100]
    institution = next((b["text"] for p in pages[:2] for b in p["blocks"]
                        if "COLLEGE OF" in b["text"].upper() or "UNIVERSITY OF" in b["text"].upper()), "")
    department = next((b["text"] for p in pages[:2] for b in p["blocks"]
                       if b["text"].upper().startswith("DEPARTMENT OF")), "")
    images = sum(p.get("image_count", 0) for p in pages)
    return {"page_count": len(pages), "image_count": images,
            "source_title": old_title, "source_name": old_name, "source_usn": old_usn,
            "institution": institution, "department": department,
            "sections": [{"page": i + 1, "name": section_of(p)}
                         for i, p in enumerate(pages) if section_of(p)],
            "warnings": (["Original photographs and evidence images are retained unchanged; verify relevance or replace them before submission."] if images else [])}


def content_slots(template: dict) -> list[dict]:
    """Select only paragraph lines; never headings, institutional details or table cells."""
    result = []
    for idx, page in enumerate(template["pages"]):
        section = section_of(page)
        if section not in BODY.values():
            continue
        heading_ends = [b["rect"][3] for b in page["blocks"]
                        if b["text"].strip().lower() == section.lower()]
        bottom = max(heading_ends) if heading_ends else (55 if section == "Abstract" else 105)
        slots = []
        for b in page["blocks"]:
            txt = b["text"].strip()
            if b["rect"][1] < bottom + 2 or b["rect"][3] > page["height"] - 60:
                continue
            if len(txt) < 12 or not re.search(r"[a-zA-Z]", txt):
                continue
            if any(token in txt.lower() for token in LOCKED):
                continue
            if txt.lower().startswith(("chapter ", "chapter-", "fig:", "fig ")):
                continue
            slots.append({"block_id": b["block_id"], "length": len(txt), "sample": txt[:220]})
        if slots:
            result.append({"page_index": idx, "section": section, "slots": slots})
    return result


def _distribute(text: str, blocks: list[dict]) -> list[str]:
    """Pack new words inside the template's existing per-line length budget."""
    words, result = text.split(), []
    for i, block in enumerate(blocks):
        if i == len(blocks) - 1:
            result.append(" ".join(words))
            break
        line, budget = [], len(block["text"])
        while words and len(" ".join(line + [words[0]])) <= budget:
            line.append(words.pop(0))
        if not line and words:
            line.append(words.pop(0))
        result.append(" ".join(line))
    return result


def static_replacements(template: dict, details: dict, title: str, name: str, usn: str) -> dict:
    updates = {}
    old_title, old_name, old_usn = (details.get(k, "") for k in ("source_title", "source_name", "source_usn"))
    for index, page in enumerate(template["pages"]):
        kind = section_of(page)
        official = {}
        if kind in ("certificate", "declaration"):
            blocks = page["blocks"]
            if kind == "certificate":
                signature_y = min((b["rect"][1] for b in blocks
                                   if "Name & Signature" in b["text"]), default=page["height"] * .65)
                editable = [b for b in blocks if .24 * page["height"] < b["rect"][1] < signature_y - 8
                            and len(b["text"]) > 35 and "Name & Signature" not in b["text"]]
                statement = (
                    f"DRAFT FOR FACULTY REVIEW. This report concerns {title} and was prepared for "
                    f"review by {name} ({usn}). Institutional details above are retained from "
                    "the supplied reference. This draft does not certify that any activity was "
                    "performed, evaluated or approved. The student's facts and supporting evidence "
                    "must be verified by authorized faculty. Only the college can issue a "
                    "certificate, approval or signature. No institutional endorsement is "
                    "made by this draft."
                )
            else:
                editable = [b for b in blocks if .11 * page["height"] < b["rect"][1] < .31 * page["height"]
                            and len(b["text"]) > 30]
                statement = (
                    f"I, {name} ({usn}), submit this draft report entitled {title} to "
                    f"{details.get('institution') or 'the institution'} for academic review. "
                    "Project information and evidence must be checked for accuracy before "
                    "submission. This draft does not claim completed fieldwork, institutional "
                    "endorsement, faculty approval, or awarded marks. All declaration and "
                    "academic integrity obligations remain with the student and authorized staff."
                )
            official = {(index, b["block_id"]): value
                        for b, value in zip(editable, _distribute(statement, editable))}
            updates.update(official)
        for block in page["blocks"]:
            key = (index, block["block_id"])
            if key in official:
                continue
            original = block["text"]
            current = original
            for source, target in ((old_title, title), (old_name, name), (old_usn, usn)):
                if source and source.lower() in current.lower():
                    current = re.sub(re.escape(source), lambda _m: target, current, flags=re.I)
            if current != original:
                updates[key] = current
        if kind in ("references", "bibliography"):
            # Preserve headings, but do not fabricate valid citations for a new project.
            for b in page["blocks"]:
                if b["rect"][1] < 90 or b["rect"][3] > page["height"] - 60:
                    continue
                hit = re.match(r"^(\d+)[.)]\s+", b["text"])
                if hit:
                    updates[(index, b["block_id"])] = f"{hit.group(1)}. [Add a verified project reference]"
                elif len(b["text"]) > 18:
                    updates[(index, b["block_id"])] = ""
        if kind == "photographs":
            for b in page["blocks"]:
                if b["text"].lower().startswith(("fig :", "fig:", "fig2:")):
                    updates[(index, b["block_id"])] = "Reference image - replace with verified project evidence"
    return updates


def build_plan(template: dict, details: dict, title: str, name: str, usn: str,
               ai_pages: list[dict]) -> dict:
    updates = static_replacements(template, details, title, name, usn)
    editable = {(p["page_index"], slot["block_id"]): slot
                for p in content_slots(template) for slot in p["slots"]}
    rejected = 0
    for page in ai_pages:
        index = int(page.get("page_index", -1))
        for line in page.get("blocks", []):
            try:
                key = (index, int(line["block_id"]))
                value = str(line.get("text", "")).strip()
                if key not in editable or not value or len(value) > editable[key]["length"] * 1.5 + 20:
                    rejected += 1
                    continue
                updates[key] = value
            except (ValueError, TypeError, KeyError):
                rejected += 1
    return {"pages": [{"page_index": i, "blocks": [
        {"block_id": block_id, "text": value} for (p, block_id), value in updates.items() if p == i
    ]} for i in range(len(template["pages"]))],
        "planned_body_slots": len(editable),
        "rewritten_body_slots": sum(key in editable for key in updates),
        "rejected_ai_slots": rejected}
