"""Page-by-page PDF visual quality gate.

Checks the generated file against its exact source, masking only text areas
explicitly approved for rewriting. Changed text itself is not claimed to be
pixel-identical. This is layout QA, not an academic originality certificate.
"""
from __future__ import annotations

import pymupdf as fitz
from PIL import Image, ImageChops, ImageDraw


class LayoutQualityError(ValueError):
    pass


def _bitmap(page, zoom):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)


def verify_pdf_layout(source: bytes, output: bytes, template: dict, plan: dict, *, zoom: float = 0.9) -> dict:
    """Verify page geometry, image placements and unedited visual pixels."""
    edited = {}
    edited_details = {}
    for page in plan.get('pages', []):
        page_index = int(page.get('page_index', -1))
        for change in page.get('blocks', []):
            edited.setdefault(page_index, set()).add(int(change['block_id']))
            edited_details[(page_index,int(change['block_id']))]=change
    issues = []
    summary = []
    with fitz.open(stream=source, filetype='pdf') as old, fitz.open(stream=output, filetype='pdf') as new:
        if len(old) != len(new) or len(old) != template['page_count']:
            raise LayoutQualityError('Generated report has a different page count.')
        for i in range(len(old)):
            page_old, page_new = old[i], new[i]
            if (abs(page_old.rect.width-page_new.rect.width) > .1
                    or abs(page_old.rect.height-page_new.rect.height) > .1
                    or page_old.rotation != page_new.rotation):
                issues.append(f'Page {i+1}: paper size or rotation changed')
                continue
            old_images, new_images = page_old.get_image_info(), page_new.get_image_info()
            if len(old_images) != len(new_images):
                issues.append(f'Page {i+1}: embedded picture placements changed')
            else:
                for original, updated in zip(old_images, new_images):
                    if any(abs(float(a)-float(b)) > .5 for a,b in zip(original['bbox'],updated['bbox'])):
                        issues.append(f'Page {i+1}: a picture moved from its reference position')
                        break
            old_bitmap, new_bitmap = _bitmap(page_old, zoom), _bitmap(page_new, zoom)
            if old_bitmap.size != new_bitmap.size:
                issues.append(f'Page {i+1}: rendered canvas dimensions changed')
                continue
            white = Image.new('L', old_bitmap.size, 255)
            painter = ImageDraw.Draw(white)
            for line in template['pages'][i]['blocks']:
                if line['block_id'] in edited.get(i, set()):
                    x0,y0,x1,y1 = line['rect']
                    actual = edited_details.get((i,line['block_id']),{}).get('visual_mask')
                    if actual:
                        x0=min(x0,actual[0]); y0=min(y0,actual[1])
                        x1=max(x1,actual[2]); y1=max(y1,actual[3])
                    painter.rectangle((int(x0*zoom)-3,int(y0*zoom)-3,int(x1*zoom)+3,int(y1*zoom)+3), fill=0)
            diff = ImageChops.difference(old_bitmap, new_bitmap).convert('L')
            distinct = diff.point(lambda v: 255 if v > 23 else 0)
            outside = ImageChops.multiply(distinct, white)
            pixel_count = outside.histogram()[255]
            outside_count = max(1, white.histogram()[255])
            ratio = pixel_count / outside_count
            summary.append({'page':i+1,'outside_edit_difference_pct':round(ratio*100,3),
                            'source_images':len(old_images),'revised_images':len(new_images)})
            if ratio > 0.005:
                issues.append(f'Page {i+1}: {ratio*100:.2f}% unexpected visual difference outside allowed text')
    result = {'status':'passed' if not issues else 'failed','pages_checked':len(summary),
              'changed_lines':sum(map(len,edited.values())),'page_checks':summary,
              'max_outside_edit_difference_pct':max((p['outside_edit_difference_pct'] for p in summary), default=0),
              'issues':issues[:12]}
    if issues:
        raise LayoutQualityError('Visual format check failed: ' + '; '.join(issues[:3]))
    return result
