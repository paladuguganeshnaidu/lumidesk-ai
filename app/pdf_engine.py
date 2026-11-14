from __future__ import annotations
import io
import re
from pathlib import Path
import pymupdf as fitz
from .typography import annotate_typography
from .format_fit import fit_body_text

FONT_DIR = Path(__file__).resolve().parent / "fonts"

MAX_PAGES = 70
MAX_BLOCKS = 16000


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
                    "origin": [round(float(v), 2) for v in primary.get("origin", (rect.x0, rect.y1))],
                    "spans": [{"text": part.get("text", ""), "font": str(part.get("font", ""))[:85],
                               "size_pt": round(float(part.get("size", 10)), 2),
                               "color": int(part.get("color", 0)), "flags": int(part.get("flags", 0)),
                               "origin": [round(float(v), 2) for v in part.get("origin", (rect.x0, rect.y1))],
                               "bbox": [round(float(v), 2) for v in part.get("bbox", rect)]}
                              for part in spans[:12]],
                    "font": str(primary.get("font", "Helvetica"))[:60],
                    "color": int(primary.get("color", 0)), "flags": int(primary.get("flags", 0)),
                    "leading_spaces": len(raw_text) - len(raw_text.lstrip()),
                })
        total_blocks += len(blocks)
        if total_blocks > MAX_BLOCKS:
            doc.close()
            raise ValueError(f"This PDF has more than {MAX_BLOCKS} text lines; simplify dense pages or split the document.")
        page_data = {
            "page_index": page_index,
            "width": round(page.rect.width, 2),
            "height": round(page.rect.height, 2),
            "image_count": len(page.get_images(full=True)),
            "blocks": blocks,
            "rotation": int(page.rotation),
        }
        annotate_typography(page_data)
        pages.append(page_data)
    if total_blocks == 0:
        doc.close()
        raise ValueError("No selectable text was found. This is an image-only or scanned PDF. Upload a searchable PDF with selectable text; exact text and font reconstruction cannot be guaranteed from scans.")
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
                replacements[(pidx, int(item["block_id"]))] = {"text": _clean_text(str(item.get("text", ""))), "body": bool(item.get("body")), "justify": False, "plan_item": item}
            except (KeyError, TypeError, ValueError):
                continue
    warnings = []
    # Reuse the actual embedded TrueType face from the input when it can render
    # the new glyphs. This preserves exact family/weight/character metrics far
    # better than forcing a generic fallback font in every replacement line.
    embedded_fonts = {}
    for idx in range(len(doc)):
        for entry in doc[idx].get_fonts(full=True):
            xref, ext, family, pdf_name = entry[:4]
            face = str(pdf_name).split('+')[-1].lower().replace('-', '')
            if ext not in ('ttf', 'otf') or face in embedded_fonts:
                continue
            try:
                data = doc.extract_font(xref)[3]
                if data:
                    embedded_fonts[face] = (fitz.Font(fontbuffer=data), data)
            except Exception:
                pass
    for page_info in template["pages"]:
        pidx = page_info["page_index"]
        page = doc[pidx]
        original_blocks = page_info["blocks"]
        changed = [block for block in original_blocks if (pidx, block["block_id"]) in replacements]
        if not changed:
            continue  # Untouched headings, institutional text, graphics, and photos stay intact.
        # Use the embedded source font's real glyph advances when reflowing
        # the generated paragraphs into the original number of line slots.
        block_fonts = {}
        for block in changed:
            face = str(block.get('font','')).lower().replace('-', '')
            original = embedded_fonts.get(face)
            block_fonts[block['block_id']] = (
                original[0] if original else fitz.Font(_font_name(block['font'], block['flags']))
            )
        by_id = {b['block_id']: replacements[(pidx,b['block_id'])] for b in changed}
        warnings.extend(f"Page {pidx+1}: {note}" for note in
                        fit_body_text(page_info, by_id, block_fonts))
        for block in changed:
            rect = fitz.Rect(block["rect"])
            # A little horizontal padding prevents an old final glyph surviving.
            rect.x0 = max(0, rect.x0 - 0.4)
            rect.x1 = min(page.rect.width, rect.x1 + 0.4)
            page.add_redact_annot(rect, fill=None, cross_out=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        for block in changed:
            item = replacements[(pidx, block["block_id"])]
            text = item["text"].strip()
            if not text:
                continue
            rect = fitz.Rect(block["rect"])
            font_size = min(24, max(7.0, float(block["font_size"])))
            tint = int(block["color"])
            color = (((tint >> 16) & 255) / 255, ((tint >> 8) & 255) / 255, (tint & 255) / 255)
            oldfont = _font_name(block["font"], block["flags"])
            original_name = str(block.get('font', '')).lower().replace('-', '')
            original = embedded_fonts.get(original_name)
            # Always prefer an exact embedded source font when its glyph set
            # includes the generated text. Some PDFs embed only partial subsets.
            if original and all((c in '\n\r\t' or original[0].has_glyph(ord(c))) for c in text):
                font, binary = original
                fontname = f"LumiOrig{pidx}{block['block_id']}"
                page.insert_font(fontname=fontname, fontbuffer=binary)
            else:
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
            # Do not shrink the reference type size (old code changed 16pt
            # cover titles to 10pt). Preserve the actual font point size.
            target_size = font_size
            text_width = font.text_length(text, fontsize=target_size)
            centered = (pidx == 0 and
                        abs((rect.x0+rect.x1)/2-page.rect.width/2) < page.rect.width*.20 and
                        (block.get('font_size',0)>=12 or block.get('leading_spaces',0)>=8))
            centered = centered or bool(block.get('leading_spaces',0)>=8)
            if centered:
                visual_center=(rect.x0+rect.x1)/2
                if block.get('leading_spaces',0)>=8:
                    visual_center=page.rect.width/2
                start_x=visual_center-text_width/2
                if start_x<24 or start_x+text_width>page.rect.width-24:
                    raise ValueError(
                        f"Page {pidx+1}: requested title or name cannot fit at the "
                        f"original {font_size:g} pt. Please shorten it for an exact match.")
            else:
                start_x=rect.x0
                available=(rect.width+5 if item.get('body')
                           else max(rect.width+6,page.rect.width-start_x-30))
                if text_width>available+.4 and not item.get('justify'):
                    if item.get('body'):
                        # In a provider-fallback draft, keep the original point
                        # size and a readable last complete word. Do not shrink
                        # the type or let characters overlap the right margin.
                        while text_width>available+.4 and len(text.split())>1:
                            text=' '.join(text.split()[:-1])
                            text_width=font.text_length(text,fontsize=target_size)
                        item['plan_item']['text']=text
                        warnings.append(
                            f"Page {pidx+1}, line {block['block_id']+1}: "
                            "draft body prose shortened to preserve original font and margins; review wording.")
                        if text_width>available+.4:
                            raise ValueError(
                                f"Page {pidx+1}: one long word cannot fit the original line.")
                    else:
                        # Repeated running titles/captions may have less room than
                        # the cover title. Avoid changing font metrics or page
                        # geometry; shorten only secondary text, never an ID.
                        if re.search(r"\b\d[A-Za-z]{2}\d{2}[A-Za-z]{2}\d{3}\b", text):
                            raise ValueError(
                                f"Page {pidx+1}: registration number would be clipped at the source font size.")
                        while text_width>available+.4 and len(text.split())>1:
                            text=' '.join(text.split()[:-1])
                            text_width=font.text_length(text,fontsize=target_size)
                        item['plan_item']['text']=text
                        warnings.append(
                            f"Page {pidx+1}, line {block['block_id']+1}: secondary "
                            "caption shortened to retain the original font and page margin.")
                        if text_width>available+.4:
                            raise ValueError(
                                f"Page {pidx+1}: the replacement cannot fit the original text area.")
            original_baseline = block.get('origin', [start_x, rect.y1])
            baseline = float(original_baseline[1])
            # Directly retain the PDF baseline. If shrinking text is necessary,
            # keep the baseline fixed rather than shifting the whole line.
            point = fitz.Point(start_x, max(1.0, min(page.rect.height-1, baseline)))
            # The visual QA masks the union of the original and the rewritten
            # text footprint (important for center-aligned title changes).
            item['plan_item']['visual_mask']=[
                min(rect.x0,start_x)-3,rect.y0-3,
                max(rect.x1,start_x+text_width)+3,rect.y1+3]
            if item.get('justify'):
                words=text.split()
                widths=[font.text_length(word,fontsize=target_size) for word in words]
                gap=(rect.width-sum(widths))/(len(words)-1) if len(words)>1 else 0
                usual=font.text_length(' ',fontsize=target_size)
                if len(words)>=3 and .55*usual<=gap<=3.5*usual:
                    xpos=start_x
                    for word,width in zip(words,widths):
                        page.insert_text((xpos,point.y),word,fontname=fontname,
                                         fontsize=target_size,color=color,overlay=True)
                        xpos+=width+gap
                else:
                    warnings.append(f"Page {pidx+1}: natural justification did not fit this line.")
                    page.insert_text(point,text,fontname=fontname,
                                     fontsize=target_size,color=color,overlay=True)
            else:
                page.insert_text(point,text,fontname=fontname,fontsize=target_size,
                                 color=color,overlay=True)
    doc.set_metadata({"title": "LumiDesk AI — Student Project Report", "author": "Student-authored report", "subject": "Original project report generated from an authorized design template"})
    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return out, warnings[:30]
