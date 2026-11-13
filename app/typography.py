"""Inspect measurable PDF typography without pretending that PDF exposes Word styles.

PDF coordinates are in points (1/72 inch). A PDF describes glyph positions,
not necessarily the author's paragraph spacing or original Word style name.
We report measured baselines, glyph boxes, font names and estimated leading.
"""
from __future__ import annotations
from collections import Counter
from statistics import median
from typing import Any


def _num(value: float | int, digits=2) -> float:
    return round(float(value), digits)


def font_family(pdf_name: str) -> str:
    """Map embedded PostScript face names to editable Word family names."""
    face = str(pdf_name or '').split('+')[-1]
    lowered = face.lower().replace(' ', '').replace('-', '')
    if 'timesnewroman' in lowered or 'timesroman' in lowered:
        return 'Times New Roman'
    if any(x in lowered for x in ('arial', 'helvetica', 'liberationsans')):
        return 'Arial'
    if 'calibri' in lowered or 'carlito' in lowered:
        return 'Calibri'
    if 'cambria' in lowered:
        return 'Cambria'
    if 'georgia' in lowered:
        return 'Georgia'
    if 'courier' in lowered:
        return 'Courier New'
    if 'noto' in lowered:
        return face.split('-')[0]
    return face or 'Times New Roman'


def annotate_typography(page: dict) -> dict:
    """Add measured font, baseline, approximate leading, margins and alignment."""
    width, height = float(page['width']), float(page['height'])
    blocks = page['blocks']
    sorted_lines = sorted(
        (b for b in blocks if float(b.get('font_size', 0)) >= 5 and 3 < b['rect'][1] < height - 3),
        key=lambda b: (round(b['rect'][1] / 3), b['rect'][0]),
    )
    lefts = [b['rect'][0] for b in sorted_lines if len(b.get('text', '')) >= 22 and b['rect'][0] < width * .65]
    rights = [width - b['rect'][2] for b in sorted_lines if len(b.get('text', '')) >= 22 and b['rect'][2] > width * .35]
    left = _num(median(lefts)) if lefts else 0.0
    right = _num(median(rights)) if rights else 0.0
    top = _num(min((b['rect'][1] for b in sorted_lines), default=0))
    bottom = _num(height - max((b['rect'][3] for b in sorted_lines), default=height))
    line_gaps = []
    for i, current in enumerate(sorted_lines):
        x0, y0, x1, y1 = current['rect']
        width_line = x1 - x0
        # X alignment measured from occupied glyph coordinates; PDF may omit paragraph markers.
        near_center = abs((x0 + x1) / 2 - width / 2) < min(25, width * .055)
        near_right = abs(width - x1 - right) < 14
        if width_line < width * .83 and near_center:
            alignment = 'center'
        elif width_line < width * .56 and near_right and x0 > width * .35:
            alignment = 'right'
        else:
            alignment = 'left'
        current['alignment'] = alignment
        current['font_family'] = font_family(current.get('font', ''))
        # Baseline and glyph bounding boxes come directly from PDF spans when present.
        current['origin'] = [_num(n) for n in current.get('origin', [x0, y1 - max(1, current['font_size'] * .2)])]
        # Only adjacent lines of comparable text area yield useful line spacing data.
        previous = sorted_lines[i - 1] if i else None
        current['line_spacing_pt'] = None
        if previous is not None and abs(previous['rect'][0] - x0) < max(35, width * .06):
            diff = float(current['origin'][1]) - float(previous['origin'][1])
            if max(5, current['font_size'] * .8) <= diff <= max(38, current['font_size'] * 4):
                current['line_spacing_pt'] = _num(diff)
                line_gaps.append(diff)
    fonts = Counter((b.get('font_family', font_family(b.get('font', ''))), round(float(b.get('font_size', 0)), 1)) for b in sorted_lines)
    page['typography'] = {
        'units': 'pt',
        'page_size_pt': [_num(width), _num(height)],
        'measured_margins_pt': {'left': left, 'right': right, 'top': top, 'bottom': bottom},
        'median_baseline_spacing_pt': _num(median(line_gaps)) if line_gaps else None,
        'font_styles': [
            {'family': name, 'size_pt': size, 'text_lines': count}
            for (name, size), count in fonts.most_common(10)
        ],
        'text_line_count': len(sorted_lines),
    }
    return page['typography']


def summarize_typography(template: dict) -> dict[str, Any]:
    pages = template.get('pages', [])
    font_counter = Counter()
    leading = []
    for page in pages:
        stats = page.get('typography') or annotate_typography(page)
        for b in page.get('blocks', []):
            family = b.get('font_family', font_family(b.get('font', '')))
            size = round(float(b.get('font_size', 10)), 1)
            if size >= 5:
                font_counter[(family, size)] += 1
            if b.get('line_spacing_pt'):
                leading.append(b['line_spacing_pt'])
    primary = font_counter.most_common(8)
    return {
        'units': 'pt', 'pages': len(pages),
        'font_styles': [{'family': name, 'size_pt': size, 'text_lines': count} for (name, size), count in primary],
        'typical_line_spacing_pt': _num(median(leading)) if leading else None,
        'first_page': pages[0].get('typography', {}) if pages else {},
        'extraction_note': 'Fonts, sizes, baselines and text bounding boxes are PDF measurements. Word paragraph styles and kerning are estimated; exact cross-format equivalence is not guaranteed.',
    }
