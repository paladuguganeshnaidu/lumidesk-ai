from __future__ import annotations
import io
import re
from pathlib import Path
import pymupdf as fitz

FONT_DIR = Path(__file__).resolve().parent / "fonts"

MAX_PAGES = 35
MAX_BLOCKS = 160


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
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            lines = block.get("lines", [])
            spans = [s for line in lines for s in line.get("spans", []) if _clean_text(s.get("text", ""))]
            if not spans:
                continue
            text = _clean_text(" ".join(s.get("text", "") for s in spans))
            rect = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
            if not text or rect.is_empty or rect.width < 4 or rect.height < 3:
                continue
            # Skip text that is likely a page number; it is preserved as a structural marker.
            if len(text) <= 3 and text.isdigit():
                continue
            first = spans[0]
            blocks.append({
                "block_id": len(blocks),
                "text": text[:190],
                "rect": [round(v, 2) for v in rect],
                "font_size": round(float(first.get("size", 10)), 1),
                "font": str(first.get("font", "Helvetica"))[:60],
                "color": int(first.get("color", 0)),
                "flags": int(first.get("flags", 0)),
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
        original_blocks = page_info["blocks"]
        # Remove only text; preserve raster images and vector artwork, including logos.
        for block in original_blocks:
            rect = fitz.Rect(block["rect"])
            page.add_redact_annot(rect, fill=None, cross_out=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        for block in original_blocks:
            text = replacements.get((pidx, block["block_id"]), "")
            if not text:
                text = "[STUDENT TO COMPLETE]"
                warnings.append(f"Page {pidx + 1}, text area {block['block_id'] + 1}: the AI did not return content for this area.")
            rect = fitz.Rect(block["rect"])
            # Slight inset prevents descenders from touching neighboring artwork.
            rect.x0 += 0.6; rect.y0 += 0.4; rect.x1 -= 0.6; rect.y1 -= 0.4
            fontsize = max(5.2, min(float(block["font_size"]), 24.0))
            color_int = int(block["color"])
            color = (((color_int >> 16) & 255) / 255, ((color_int >> 8) & 255) / 255, (color_int & 255) / 255)
            font = _font_name(block["font"], block["flags"])
            codepoints = [ord(ch) for ch in text]
            if any(0x0C00 <= cp <= 0x0C7F for cp in codepoints):
                fontfile = FONT_DIR / "NotoSansTelugu-Regular.ttf"
                custom_font = "LumiTelugu"
            elif any(0x0900 <= cp <= 0x097F for cp in codepoints):
                fontfile = FONT_DIR / "NotoSansDevanagari-Regular.ttf"
                custom_font = "LumiDevanagari"
            else:
                fontfile = FONT_DIR / ("NotoSans-Bold.ttf" if (block["flags"] & 16 or "bold" in block["font"].lower()) else "NotoSans-Regular.ttf")
                custom_font = "LumiSans"
            result = -1
            use_embedded_font = fontfile.exists()
            if not use_embedded_font and any(ord(ch) > 255 for ch in text):
                warnings.append(f"Page {pidx + 1}, text area {block['block_id'] + 1}: this deployment does not include the required Indic font; use Latin text or bundle the Noto font files.")
                text = ''.join(ch if ord(ch) <= 255 else ' ' for ch in text)
            for _ in range(9):
                if use_embedded_font:
                    result = page.insert_textbox(rect, text, fontname=custom_font, fontfile=str(fontfile), fontsize=fontsize, color=color, align=fitz.TEXT_ALIGN_LEFT, lineheight=1.08, overlay=True)
                else:
                    result = page.insert_textbox(rect, text, fontname=font, fontsize=fontsize, color=color, align=fitz.TEXT_ALIGN_LEFT, lineheight=1.08, overlay=True)
                if result >= 0:
                    break
                # Remove a failed insertion before retrying: insert_textbox returns negative without drawing.
                fontsize *= 0.88
            if result < 0:
                warnings.append(f"Page {pidx + 1}, text area {block['block_id'] + 1}: content may not fit the original space.")
                short = text[:max(25, int(len(text) * 0.65))].rstrip()
                page.insert_textbox(rect, short + "…", fontname=custom_font, fontfile=str(fontfile), fontsize=max(5, fontsize), color=color, overlay=True) if use_embedded_font else page.insert_textbox(rect, short + "…", fontname=font, fontsize=max(5, fontsize), color=color, overlay=True)
    doc.set_metadata({"title": "LumiDesk AI — Student Project Report", "author": "Student-authored report", "subject": "Original project report generated from an authorized design template"})
    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return out, warnings[:30]
