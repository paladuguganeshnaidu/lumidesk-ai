from __future__ import annotations
import io
import re
from pathlib import Path
import pymupdf as fitz

FONT_DIR = Path(__file__).resolve().parent / "fonts"

MAX_PAGES = 70
MAX_BLOCKS = 6500


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def inspect_pdf(data: bytes) -> tuple[fitz.Document, dict]:
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.is_encrypted:
        raise ValueError("Password-protected PDFs are not supported. Upload an unlocked copy.")
    if not 1 <= len(doc) <= MAX_PAGES:
        raise ValueError(f"Please upload a PDF with 1–{MAX_PAGES} pages for this report editor.")
    pages = []
    total_blocks = 0
    for page_index, page in enumerate(doc):
        blocks = []
        raw = page.get_text("dict")
        # Treat each PDF text line as an independent slot. Multi-line PDF blocks
        # often mix a chapter heading and the first paragraph; editing the whole
        # block would destroy the unchanged heading and its typography.
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
                    "color": int(primary.get("color", 0)), "flags": int(primary.get("flags", 0)),
                    "leading_spaces": len(raw_text) - len(raw_text.lstrip()),
                })
        total_blocks += len(blocks)
        if total_blocks > MAX_BLOCKS:
            doc.close()
            raise ValueError(f"This PDF has more than {MAX_BLOCKS} text lines; simplify dense pages or split the document.")
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
        original_blocks = page_info["blocks"]
        changed = [block for block in original_blocks if (pidx, block["block_id"]) in replacements]
        if not changed:
            continue  # Untouched headings, institutional text, graphics, and photos stay intact.
        for block in changed:
            rect = fitz.Rect(block["rect"])
            # A little horizontal padding prevents an old final glyph surviving.
            rect.x0 = max(0, rect.x0 - 0.4)
            rect.x1 = min(page.rect.width, rect.x1 + 0.4)
            page.add_redact_annot(rect, fill=None, cross_out=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        for block in changed:
            text = replacements[(pidx, block["block_id"])].strip()
            if not text:
                continue
            rect = fitz.Rect(block["rect"])
            font_size = min(24, max(7.0, float(block["font_size"])))
            tint = int(block["color"])
            color = (((tint >> 16) & 255) / 255, ((tint >> 8) & 255) / 255, (tint & 255) / 255)
            oldfont = _font_name(block["font"], block["flags"])
            if any(0x0C00 <= ord(c) <= 0x0C7F for c in text):
                fontfile = FONT_DIR / "NotoSansTelugu-Regular.ttf"
                custom = "LumiTelugu"
            elif any(0x0900 <= ord(c) <= 0x097F for c in text):
                fontfile = FONT_DIR / "NotoSansDevanagari-Regular.ttf"
                custom = "LumiDevanagari"
            else:
                fontfile = FONT_DIR / ("NotoSans-Bold.ttf" if (block["flags"] & 16 or "bold" in block["font"].lower()) else "NotoSans-Regular.ttf")
                custom = "LumiSans"
            embedded = fontfile.exists()
            if embedded:
                font = fitz.Font(fontfile=str(fontfile))
                page.insert_font(fontname=custom, fontfile=str(fontfile))
                fontname = custom
            else:
                font = fitz.Font(oldfont)
                fontname = oldfont
                if any(ord(c) > 255 for c in text):
                    warnings.append(f"Page {pidx+1}: non-Latin font unavailable. Check text rendering.")
            # Match the original line's occupied area rather than reflowing the whole page.
            width_available = min(page.rect.width - rect.x0 - 30, rect.width + 3)
            target_size = font_size
            for _ in range(15):
                if font.text_length(text, fontsize=target_size) <= width_available:
                    break
                target_size *= 0.97
            if font.text_length(text, fontsize=target_size) > width_available:
                # No silent clipping: leave a fit warning if the target was too long.
                warnings.append(f"Page {pidx + 1}, line {block['block_id'] + 1}: replacement needs manual fitting.")
                target_size = max(5.7, target_size)
                while font.text_length(text, fontsize=target_size) > width_available and len(text) > 12:
                    text = text.rsplit(" ", 1)[0] if " " in text else text[:-1]
            start_x = rect.x0
            if block.get("leading_spaces", 0) >= 8:
                # Word-like PDFs sometimes encode centered names with 50+ leading
                # spaces inside one span. The line bbox starts at the left margin,
                # but the visible word was centered; respect its actual anchor.
                original_font = fitz.Font(_font_name(block["font"], block["flags"]))
                original_width = original_font.text_length(block["text"], fontsize=float(block["font_size"]))
                visual_center = rect.x1 - original_width / 2
                start_x = max(10, visual_center - font.text_length(text, fontsize=target_size) / 2)
            point = fitz.Point(start_x, rect.y1 - max(0.7, (rect.height - target_size) * 0.11))
            page.insert_text(point, text, fontname=fontname, fontsize=target_size, color=color, overlay=True)
    doc.set_metadata({"title": "LumiDesk AI — Student Project Report", "author": "Student-authored report", "subject": "Original project report generated from an authorized design template"})
    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return out, warnings[:30]
