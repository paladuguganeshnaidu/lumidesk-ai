"""Replace individual image placements in a completed report without changing its text.

The original completed PDF remains the immutable edit base. The UI chooses a
specific page/image placement, not a global image XREF, since shared images can
appear on several pages. Never accept SVG, URLs, or unbounded raster payloads.
"""
from __future__ import annotations

import io
import re
from typing import Mapping

import pymupdf as fitz
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 18_000_000
MAX_REPLACEMENTS = 100
SLOT_RE = re.compile(r"^p(\d+)-i(\d+)$")
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def image_slots(pdf: bytes) -> list[dict]:
    """Return stable placement IDs and original bounding boxes in page order."""
    slots = []
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        for pageno, page in enumerate(doc):
            # get_image_info locates each *displayed* image, including reused XREFs.
            candidates = []
            for info in page.get_image_info(xrefs=True):
                rect = fitz.Rect(info['bbox']) & page.rect
                if rect.is_empty or rect.width < 14 or rect.height < 14:
                    continue
                candidates.append((rect, info))
            # The order is visual top-to-bottom, left-to-right, independent of XREFs.
            candidates.sort(key=lambda pair: (round(pair[0].y0, 1), round(pair[0].x0, 1)))
            for index, (rect, info) in enumerate(candidates):
                slots.append({
                    'id': f'p{pageno}-i{index}', 'page': pageno + 1,
                    'index': index + 1,
                    'rect': [round(x, 2) for x in rect],
                    'width': int(info.get('width', 0)),
                    'height': int(info.get('height', 0)),
                })
    return slots


def normalize_upload(payload: bytes) -> bytes:
    """Validate image bounds, remove EXIF/metadata, and encode a safe RGB JPEG."""
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ValueError('Choose a photo smaller than 8 MB.')
    try:
        with Image.open(io.BytesIO(payload)) as original:
            if original.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Only JPEG, PNG and WebP images are supported.')
            if original.width * original.height > MAX_IMAGE_PIXELS or min(original.size) < 12:
                raise ValueError('Image dimensions must be at least 12 pixels and under 18 megapixels.')
            original.load()
            image = ImageOps.exif_transpose(original)
            if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                rgba = image.convert('RGBA')
                backdrop = Image.new('RGB', rgba.size, 'white')
                backdrop.paste(rgba, mask=rgba.getchannel('A'))
                image = backdrop
            else:
                image = image.convert('RGB')
            # Limit size stored into PDF while retaining enough pixels for print.
            image.thumbnail((2500, 2500), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=91, optimize=True, progressive=False)
            return output.getvalue()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError('The selected image cannot be decoded safely. Choose a JPEG, PNG or WebP photo.') from exc


def apply_replacements(original_pdf: bytes, replacements: Mapping[str, bytes]) -> bytes:
    """Replace chosen visual slots, preserving page geometry and every text line."""
    if not replacements:
        return original_pdf
    if len(replacements) > MAX_REPLACEMENTS:
        raise ValueError(f'You may replace up to {MAX_REPLACEMENTS} image placements per report.')
    known = {slot['id']: slot for slot in image_slots(original_pdf)}
    if not set(replacements).issubset(known):
        raise ValueError('Image slot not found. Refresh the editor and try again.')
    with fitz.open(stream=original_pdf, filetype='pdf') as doc:
        # Record text before editing; fail rather than silently alter any lines.
        before = [page.get_text('text') for page in doc]
        for image_id, jpeg in replacements.items():
            slot = known[image_id]
            page = doc[slot['page'] - 1]
            rect = fitz.Rect(slot['rect'])
            # Remove image pixels only, not text and not vector borders/captions.
            page.add_redact_annot(rect, fill=(1, 1, 1), cross_out=False)
            page.apply_redactions(images=1, graphics=1, text=1)
            # Center-crop into the *same* existing page coordinates.
            with Image.open(io.BytesIO(jpeg)) as img:
                ratio = rect.width / rect.height
                width, height = img.size
                if width / height > ratio:
                    side = int(round(height * ratio)); left = (width - side) // 2
                    img = img.crop((left, 0, left + side, height))
                else:
                    side = int(round(width / ratio)); top = (height - side) // 2
                    img = img.crop((0, top, width, top + side))
                buffer = io.BytesIO()
                img.save(buffer, format='JPEG', quality=91)
            page.insert_image(rect, stream=buffer.getvalue(), overlay=True, keep_proportion=False)
        after = [page.get_text('text') for page in doc]
        if before != after:
            raise ValueError('The edit would change text. Nothing was saved.')
        result = doc.tobytes(garbage=4, deflate=True, clean=True)
    with fitz.open(stream=result, filetype='pdf') as check:
        if len(check) != len(before) or [p.get_text('text') for p in check] != before:
            raise ValueError('The edited PDF failed its text preservation check.')
    return result


def slot_preview(pdf: bytes, slot: dict, *, max_dimension: int = 500) -> bytes:
    """Preview only the selected position without exposing unrelated pages."""
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        page = doc[slot['page'] - 1]
        rect = fitz.Rect(slot['rect'])
        scale = min(2.0, max_dimension / max(1, rect.width, rect.height))
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect,
                               alpha=False).tobytes('png')
