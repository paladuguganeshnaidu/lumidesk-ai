from __future__ import annotations
import io
import re
from pathlib import Path
import pymupdf as fitz

FONT_DIR = Path(__file__).resolve().parent / "fonts"

MAX_PAGES = 35
MAX_BLOCKS = 700


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def inspect_pdf(data: bytes) -> tuple[fitz.Document, dict]:
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.is_encrypted:
        raise ValueError("Password-protected PDFs are not supported. Upload an unlocked copy.")
    if not 1 <= len(doc) <= MAX_PAGES:
        raise ValueError(f"Please upload a PDF with 1–{MAX_PAGES} pages for this free-tier workflow.")
    pages = []
    total_blocks = 0
    for page_index, page in enumerate(doc):
        blocks = []
        raw = page.get_text("dict")
        # The PDF may mix a heading and the first body paragraph in one
        # text block. Treat each actual visible line as its own editable slot.
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = [part for part in line.get("spans", []) if _clean_text(part.get("text", ""))]
                if not spans:
                    continue
                raw_text = "".join(part.get("text", "") for part in spans)
                text = _clean_text(raw_text)
                rect = fitz.Rect(line.get("bbox", block.get("bbox", (0, 0, 0, 0))))
                if not text or rect.is_empty or rect.width < 4 or rect.height < 3:
                    continue
                if len(text) <= 3 and text.isdigit():
                    continue
                primary = max(spans, key=lambda part: len(part.get("text", "").strip()))
                blocks.append({
                    "block_id": len(blocks), "text": text[:300],
                    "rect": [round(v, 2) for v in rect],
                    "font_size": round(float(primary.get("size", 10)), 2),
                    "font": str(primary.get("font", "Helvetica"))[:60],
                    "color": int(primary.get("color", 0)),
                    "flags": int(primary.get("flags", 0)),
                    "leading_spaces": len(raw_text) - len(raw_text.lstrip()),
                })
        total_blocks += len(blocks)
        if total_blocks > MAX_BLOCKS:
            doc.close()
            raise ValueError(f"This template contains more than {MAX_BLOCKS} text areas. Use a shorter template or a simplified PDF.")
        pages.append({
            "page_index": page_index,
            "width": round(page.rect.width, 2),
            "height": round(page.rect.height, 2),
            "image_count": len(page.get_images(full=True)),
            "blocks": blocks,
        })
    if total_blocks == 0:
        doc.close()
        raise ValueError("No selectable text was found. This looks like a scanned PDF; OCR support is not enabled in this free build.")
    template = {"page_count": len(doc), "pages": pages}
    return doc, template


def _font_name(original: str, flags: int) -> str:
    name = original.lower()
    bold = bool(flags & 16) or "bold" in name or "black" in name
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    if bold and italic:
        return "hebi"
    if bold:
        return "hebo"
    if italic:
        return "heit"
    return "helv"


def render_replacement_pdf(source: bytes, template: dict, plan: dict) -> tuple[bytes, list[str]]:
    doc = fitz.open(stream=source, filetype="pdf")
    replacements = {}
    for page in plan.get("pages", []):
        pidx = int(page.get("page_index", -1))
        if pidx < 0:
            continue
        for item in page.get("blocks", []):
            try:
                replacements[(pidx, int(item["block_id"]))] = _clean_text(str(item.get("text", "")))
            except (KeyError, TypeError, ValueError):
                continue
    warnings = []
    for page_info in template["pages"]:
        pidx = page_info["page_index"]
        page = doc[pidx]
        changed = [b for b in page_info["blocks"] if (pidx, b["block_id"]) in replacements]
        if not changed:
            continue  # Untouched text, artwork, logos, tables and images are preserved.
        for block in changed:
            rect = fitz.Rect(block["rect"])
            rect.x0 = max(0, rect.x0 - .4)
            rect.x1 = min(page.rect.width, rect.x1 + .4)
            page.add_redact_annot(rect, fill=None, cross_out=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        for block in changed:
            text = replacements[(pidx, block["block_id"])].strip()
            if not text:
                continue
            rect = fitz.Rect(block["rect"])
            original_size = min(24, max(7, float(block["font_size"])))
            tint = int(block["color"])
            color = (((tint >> 16) & 255) / 255,
                     ((tint >> 8) & 255) / 255, (tint & 255) / 255)
            oldfont = _font_name(block["font"], block["flags"])
            if any(0x0C00 <= ord(ch) <= 0x0C7F for ch in text):
                fontfile, custom = FONT_DIR / "NotoSansTelugu-Regular.ttf", "LumiTelugu"
            elif any(0x0900 <= ord(ch) <= 0x097F for ch in text):
                fontfile, custom = FONT_DIR / "NotoSansDevanagari-Regular.ttf", "LumiDevanagari"
            else:
                bold = bool(block["flags"] & 16 or "bold" in block["font"].lower())
                fontfile = FONT_DIR / ("NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf")
                custom = "LumiSans"
            if fontfile.exists():
                font = fitz.Font(fontfile=str(fontfile))
                page.insert_font(fontname=custom, fontfile=str(fontfile))
                fontname = custom
            else:
                font = fitz.Font(oldfont)
                fontname = oldfont
                if any(ord(ch) > 255 for ch in text):
                    warnings.append(f"Page {pidx+1}: non-Latin font unavailable. Review the text.")
            # Keep replacements in the original one-line box and font size when possible.
            width_available = min(page.rect.width - rect.x0 - 30, rect.width + 3)
            target_size = original_size
            for _ in range(15):
                if font.text_length(text, fontsize=target_size) <= width_available:
                    break
                target_size *= .97
            if font.text_length(text, fontsize=target_size) > width_available:
                warnings.append(f"Page {pidx+1}, line {block['block_id']+1}: text was too long.")
                while font.text_length(text, fontsize=target_size) > width_available and len(text) > 12:
                    text = text.rsplit(" ", 1)[0] if " " in text else text[:-1]
            start_x = rect.x0
            if block.get("leading_spaces", 0) >= 8:
                # Centered Word/PDF lines may contain 50 literal leading spaces.
                original_font = fitz.Font(oldfont)
                original_width = original_font.text_length(block["text"], fontsize=float(block["font_size"]))
                visual_center = rect.x1 - original_width / 2
                start_x = max(10, visual_center - font.text_length(text, fontsize=target_size) / 2)
            point = fitz.Point(start_x, rect.y1 - max(.7, (rect.height - target_size) * .11))
            page.insert_text(point, text, fontname=fontname, fontsize=target_size,
                             color=color, overlay=True)
    doc.set_metadata({"title": "LumiDesk AI — Student Project Report", "author": "Student-authored report", "subject": "Original project report generated from an authorized design template"})
    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return out, warnings[:30]
