"""Template-first report planning. Fixed material is never sent for rewriting."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

USN_PATTERN = re.compile(r"\b[0-9][A-Z]{2}[0-9]{2}[A-Z]{2}[0-9]{3}\b", re.I)
REPORT_SECTIONS = {
    "abstract": "Abstract",
    "introduction": "Introduction",
    "objectives": "Objectives",
    "description of the activity": "Description of the Activity",
    "description of activity": "Description of the Activity",
    "methodology": "Methodology",
    "implementation": "Implementation",
    "literature review": "Literature Review",
    "system design": "System Design",
    "results": "Results",
    "discussion": "Discussion",
    "conclusion": "Conclusion",
    "future work": "Future Work",
    "problem statement": "Problem Statement",
}
PROTECTED_SECTIONS = ("table of contents", "certificate", "acknowledgement", "acknowledgment", "declaration", "photographs", "annexure", "references", "bibliography")
LOCKED_PHRASES = ("department of", "dept of", "college of", "university", "coordinator", "principal", "prof.", "dr.", "signature", "examiner", "approved", "academic year", "submitted by", "national service scheme")


def _txt(page: dict) -> str:
    return "\n".join(block["text"] for block in page["blocks"])


def section_of(page: dict) -> str:
    # Use explicit standalone headings; scanning whole pages wrongly classifies
    # a Table of Contents as a certificate and a methodology as photographs.
    labels = [re.sub(r"\s+", " ", b["text"].strip().lower()).strip(" .:-")
              for b in page["blocks"] if len(b["text"].strip()) <= 48]
    for word in PROTECTED_SECTIONS:
        if word in labels:
            return word
    for keyword, name in REPORT_SECTIONS.items():
        if keyword in labels:
            return name
    return ""


def extract_template_details(template: dict) -> dict:
    pages = template.get("pages", [])
    cover = _txt(pages[0]) if pages else ""
    title_match = re.search(r"report\s+titled\s*[\"'“‘]?\s*([^\"'”’\n]{5,110})", cover, re.I)
    old_title = title_match.group(1).strip(" .“”'") if title_match else ""
    if not old_title:
        # Another common template layout: a quoted title beneath a cover-page heading.
        quotes = re.findall(r"[“\"]([^”\"]{9,110})[”\"]", cover)
        old_title = next((x for x in quotes if "grade" not in x.lower() and "accredited" not in x.lower()), "")
    usn_match = USN_PATTERN.search(cover)
    old_usn = usn_match.group(0) if usn_match else ""
    old_name = ""
    if usn_match:
        row = next((x.strip() for x in cover.splitlines() if old_usn.lower() in x.lower()), "")
        row = re.sub(r"\(?\s*" + re.escape(old_usn) + r"\s*\)?", "", row, flags=re.I)
        old_name = re.sub(r"[^\w .-]", "", row).strip()[:100]
    institution = next((b["text"] for p in pages[:2] for b in p["blocks"] if "COLLEGE OF" in b["text"].upper() or "UNIVERSITY OF" in b["text"].upper()), "")
    department = next((b["text"] for p in pages[:2] for b in p["blocks"] if b["text"].upper().startswith("DEPARTMENT OF")), "")
    sections = [{"page": i + 1, "name": section_of(p)} for i, p in enumerate(pages) if section_of(p)]
    images = sum(p.get("image_count", 0) for p in pages)
    return {
        "page_count": len(pages), "image_count": images,
        "source_title": old_title, "source_name": old_name, "source_usn": old_usn,
        "institution": institution, "department": department, "sections": sections,
        "warnings": (["The template contains photographs or evidence images; these remain unchanged and must be checked for relevance to the new project."] if images else []),
    }


def content_slots(template: dict) -> list[dict]:
    """Editable line slots, including continuation pages without repeated headings.

    Preserve exact chapter headings, institutional information, page numbers, tables,
    captions and signatures. Rewriting uses the original PDF as its source rather
    than reflowing the entire document.
    """
    all_pages: list[dict] = []
    current_section = ""
    for idx, page in enumerate(template["pages"]):
        explicit = section_of(page)
        if explicit:
            # A protected chapter ends the preceding editable chapter.
            current_section = explicit if explicit in REPORT_SECTIONS.values() else ""
        if not current_section:
            continue
        section = current_section
        heading_ends = [b["rect"][3] for b in page["blocks"]
                        if b["text"].strip().lower() == section.lower()]
        heading_bottom = max(heading_ends) if heading_ends else (53 if idx else 105)
        items = []
        for block in page["blocks"]:
            text = block["text"].strip()
            y0, y1 = block["rect"][1], block["rect"][3]
            if y0 < heading_bottom + 2 or y1 > page["height"] - 54:
                continue
            if len(text) < 12 or not re.search(r"[a-zA-Z]", text):
                continue
            lowered = text.lower()
            if any(x in lowered for x in LOCKED_PHRASES):
                continue
            if lowered.startswith(("chapter ", "chapter-", "fig:", "fig ", "fig2:")):
                continue
            # Do not accidentally rewrite a heading in a continuation page.
            if lowered in REPORT_SECTIONS or lowered in PROTECTED_SECTIONS:
                continue
            items.append({"block_id": block["block_id"], "length": len(text), "sample": text[:260]})
        if items:
            all_pages.append({"page_index": idx, "section": section, "slots": items})
    return all_pages


def _wrap_for_existing_lines(text: str, blocks: list[dict]) -> list[str]:
    """Reflow words into the original line budgets; never change section titles."""
    words = text.split()
    if not blocks:
        return []
    result: list[str] = []
    for index, block in enumerate(blocks):
        if index == len(blocks) - 1:
            result.append(" ".join(words))
            break
        target = len(block["text"])
        line = []
        while words and len(" ".join(line + [words[0]])) <= target:
            line.append(words.pop(0))
        if not line and words:
            line.append(words.pop(0))
        result.append(" ".join(line))
    return result


def _official_draft_replacements(page: dict, page_index: int, old: dict, title: str, name: str, usn: str) -> dict:
    """Keep headings/staff names while not fabricating a certificate of approval."""
    kind = section_of(page)
    if kind not in ("certificate", "declaration"):
        return {}
    lines = page["blocks"]
    if kind == "certificate":
        signature_y = min((b["rect"][1] for b in lines if "Name & Signature" in b["text"]), default=page["height"] * 0.65)
        body = [b for b in lines if 0.24 * page["height"] < b["rect"][1] < signature_y - 8
                and len(b["text"]) > 35 and b["text"].strip().lower() != "certificate"
                and not b["text"].startswith("Name & Signature")]
        text = (
            f"DRAFT FOR FACULTY REVIEW. This report concerns {title} and has been "
            f"prepared for review by {name} ({usn}). The institution and department "
            "shown above are retained from the reference format. This document "
            "does not confirm that the proposed activity was performed, evaluated "
            "or approved. Academic records, supporting evidence and accuracy "
            "must be checked by the student and the designated faculty members. "
            "Any certification or signature must be issued only by an authorized "
            "college representative. Until verified, this page is a draft, not a "
            "completed or officially certified document."
        )
    else:
        body = [b for b in lines if 0.11 * page["height"] < b["rect"][1] < 0.31 * page["height"]
                and len(b["text"]) > 30]
        college = old.get("institution", "the college")
        text = (
            f"I, {name} ({usn}), submit this draft report entitled {title} "
            f"to {college} for review as part of my academic work. "
            "The project description and supporting evidence must be checked "
            "for accuracy before official submission. No completed fieldwork, "
            "external endorsement, faculty approval or assessment is implied "
            "by this generated draft. All original academic integrity obligations "
            "remain with the submitting student and the authorized faculty."
        )
    wrapped = _wrap_for_existing_lines(text, body)
    return {(page_index, block["block_id"]): line for block, line in zip(body, wrapped)}

def static_replacements(template: dict, old: dict, title: str, name: str, usn: str) -> dict:
    """Change only title/identity occurrences; do not rewrite institutional lines."""
    substitutions = {}
    old_title = old.get("source_title", "")
    old_name = old.get("source_name", "")
    old_usn = old.get("source_usn", "")
    for idx, page in enumerate(template["pages"]):
        official = _official_draft_replacements(page, idx, old, title, name, usn)
        substitutions.update(official)
        for block in page["blocks"]:
            if (idx, block["block_id"]) in official:
                continue
            raw = block["text"]
            updated = raw
            if old_title and old_title.lower() in raw.lower():
                updated = re.sub(re.escape(old_title), lambda _: title, updated, flags=re.I)
            if old_name and old_name.lower() in updated.lower():
                updated = re.sub(re.escape(old_name), lambda _: name, updated, flags=re.I)
            if old_usn and old_usn.lower() in updated.lower():
                updated = re.sub(re.escape(old_usn), lambda _: usn, updated, flags=re.I)
            if updated != raw:
                substitutions[(idx, block["block_id"])] = updated
        if section_of(page) in ("references", "bibliography"):
            # New references cannot be responsibly invented from a title alone.
            for block in page["blocks"]:
                match = re.match(r"^(\d+)[.)]\s+", block["text"])
                if match and block["rect"][1] > 90:
                    substitutions[(idx, block["block_id"])] = f"{match.group(1)}. [Add a verified reference for the new project]"
                elif 90 < block["rect"][1] < page["height"] - 65 and len(block["text"]) > 18:
                    substitutions[(idx, block["block_id"])] = ""
        if section_of(page) == "photographs":
            for block in page["blocks"]:
                if block["text"].lower().startswith(("fig :", "fig:", "fig2:")):
                    substitutions[(idx, block["block_id"])] = "Reference image - replace with verified project evidence"
    return substitutions


def build_plan(template: dict, old: dict, title: str, name: str, usn: str, ai_pages: list[dict]) -> dict:
    updates = static_replacements(template, old, title, name, usn)
    approved = {(item["page_index"], slot["block_id"]): slot for item in content_slots(template) for slot in item["slots"]}
    rejected = 0
    for page in ai_pages:
        idx = int(page.get("page_index", -1))
        for block in page.get("blocks", []):
            try:
                key = (idx, int(block["block_id"]))
                if key not in approved:
                    rejected += 1
                    continue
                new_text = str(block.get("text", "")).strip()
                if not new_text or len(new_text) > int(approved[key]["length"] * 1.5) + 20:
                    rejected += 1
                    continue
                updates[key] = new_text
            except (TypeError, ValueError, KeyError):
                rejected += 1
    return {
        "pages": [{"page_index": i, "blocks": [{"block_id": j, "text": txt} for (p, j), txt in updates.items() if p == i]} for i in range(len(template["pages"]))],
        "rejected_ai_slots": rejected,
        "planned_body_slots": len(approved),
        "rewritten_body_slots": sum(key in approved for key in updates),
    }
