"""High-fidelity *editable* DOCX export of a generated PDF.

Layout uses Word-compatible page-anchored text boxes, retaining readable,
editable text in the resulting .docx. Background artwork, vector elements and
photos are rasterized from the PDF after removing text. PDF is authoritative
for exact fixed layout; Word may substitute fonts and round dimensions.
"""
from __future__ import annotations

import io
import logging
import math
import re
from lxml import etree
from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.shared import Pt
import pymupdf as fitz
from .typography import font_family

V = 'urn:schemas-microsoft-com:vml'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _sub(parent, ns: str, name: str, **attrs):
    el = etree.SubElement(parent, f'{{{ns}}}{name}')
    for k, value in attrs.items():
        el.set(f'{{{ns}}}{k}' if ns == W else k, str(value))
    return el


def _set_background_anchor(run, width_pt: float, height_pt: float) -> None:
    """Convert python-docx inline image to page-anchored, behind-text artwork."""
    inline = run._r.xpath('.//wp:inline')
    if not inline:
        return
    inline = inline[0]
    parent = inline.getparent()
    anchor = OxmlElement('wp:anchor')
    for key, value in {'distT':'0', 'distB':'0', 'distL':'0', 'distR':'0',
                       'simplePos':'0', 'relativeHeight':'0', 'behindDoc':'1',
                       'locked':'1', 'layoutInCell':'1', 'allowOverlap':'1'}.items():
        anchor.set(key, value)
    sp = OxmlElement('wp:simplePos'); sp.set('x','0'); sp.set('y','0'); anchor.append(sp)
    ph = OxmlElement('wp:positionH'); ph.set('relativeFrom','page')
    pos = OxmlElement('wp:posOffset'); pos.text='0'; ph.append(pos); anchor.append(ph)
    pv = OxmlElement('wp:positionV'); pv.set('relativeFrom','page')
    pos = OxmlElement('wp:posOffset'); pos.text='0'; pv.append(pos); anchor.append(pv)
    for child in list(inline):
        anchor.append(child)
    # Inline includes <wp:extent>, <wp:docPr> and <a:graphic>; insert wrapNone.
    ext = anchor.find(f'{{{WP}}}extent')
    if ext is not None:
        ext.set('cx',str(int(width_pt * 12700)));ext.set('cy',str(int(height_pt * 12700)))
        anchor.insert(anchor.index(ext) + 1, OxmlElement('wp:wrapNone'))
    parent.replace(inline, anchor)


def _text_box(paragraph, item: dict, number: int, page_width: float, page_height: float) -> None:
    text = str(item.get('text', '')).strip()
    if not text or not any(ch.isprintable() for ch in text):
        return
    x0, y0, x1, y1 = map(float, item['rect'])
    size = float(item.get('font_size', 10))
    if size < 3 or y0 < -1 or x0 > page_width:
        return
    family = font_family(item.get('font_family', item.get('font', '')))
    flags = int(item.get('flags', 0))
    # Word may use a different kerning engine: allow a small horizontal safety margin.
    width = min(page_width - x0, max(x1 - x0 + 2, (x1 - x0) * 1.065))
    if width < 4:
        return
    # Text box top corresponds to glyph box top, not PDF baseline; nudge for Word metrics.
    y = max(0, y0 - size * .09)
    height = min(page_height - y, max(y1 - y0 + size * .55, size * 1.33))
    if height <= 3:
        return
    shape_id = f'LumiText_{number}'
    style = (f'position:absolute;left:{x0:.2f}pt;top:{y:.2f}pt;'
             f'width:{width:.2f}pt;height:{height:.2f}pt;z-index:100;'
             'mso-position-horizontal-relative:page;mso-position-vertical-relative:page;'
             'mso-wrap-style:none;mso-fit-shape-to-text:f;visibility:visible')
    pict = etree.Element(f'{{{W}}}pict', nsmap={'v': V})
    shape = etree.SubElement(pict,f'{{{V}}}shape')
    shape.set('id',shape_id);shape.set('type','#_x0000_t202');shape.set('style',style)
    shape.set('filled','f');shape.set('stroked','f')
    textbox = etree.SubElement(shape,f'{{{V}}}textbox');textbox.set('inset','0,0,0,0')
    textbox.set('style','mso-fit-shape-to-text:f')
    content = etree.SubElement(textbox,f'{{{W}}}txbxContent')
    p = _sub(content,W,'p')
    pp = _sub(p,W,'pPr')
    spacing = _sub(pp,W,'spacing',before='0',after='0',line=str(int(size * 20)),lineRule='atLeast')
    # A line's horizontal coordinates already encode alignment, so keep it left-aligned
    # instead of introducing Word's center/right offset twice.
    _sub(pp,W,'jc',val='left')
    run = _sub(p,W,'r'); props = _sub(run,W,'rPr')
    _sub(props,W,'rFonts',ascii=family,hAnsi=family,cs=family,eastAsia=family)
    _sub(props,W,'sz',val=str(max(6,int(round(size*2)))))
    _sub(props,W,'szCs',val=str(max(6,int(round(size*2)))))
    if flags & 16 or 'bold' in item.get('font','').lower(): _sub(props,W,'b')
    if flags & 2 or 'italic' in item.get('font','').lower(): _sub(props,W,'i')
    tint = int(item.get('color',0));_sub(props,W,'color',val=f'{tint:06X}')
    t = _sub(run,W,'t'); t.set(f'{{http://www.w3.org/XML/1998/namespace}}space','preserve');t.text=text
    paragraph.add_run()._r.append(pict)


def export_editable_docx(pdf: bytes, *, max_pages: int = 70, dpi: int = 90) -> tuple[bytes, list[str]]:
    """Turn generated PDF into a page-faithful, editable DOCX.

    Page art is rasterized while text is Word-editable VML textboxes, one per
    measured PDF line. No Microsoft Word license or external conversion API.
    """
    if not pdf.startswith(b'%PDF-'):
        raise ValueError('Expected a valid generated PDF.')
    document = fitz.open(stream=pdf, filetype='pdf')
    if not (1 <= len(document) <= max_pages):
        raise ValueError(f'Word export supports 1-{max_pages} pages.')
    word = Document()
    section = word.sections[0]
    first = document[0].rect
    section.page_width = Pt(first.width);section.page_height = Pt(first.height)
    section.top_margin=Pt(0);section.bottom_margin=Pt(0)
    section.left_margin=Pt(0);section.right_margin=Pt(0)
    section.header_distance=Pt(0);section.footer_distance=Pt(0)
    style=word.styles['Normal'];style.font.name='Times New Roman';style.font.size=Pt(1)
    style.paragraph_format.space_after=Pt(0)
    word.core_properties.title = 'LumiDesk AI - Editable student report'
    word.core_properties.subject='Editable Word layout reconstructed from the generated PDF'
    warnings = []
    line_id=0
    for index in range(len(document)):
        page=document[index]
        if abs(page.rect.width-first.width)>1 or abs(page.rect.height-first.height)>1:
            warnings.append(f'Page {index+1}: custom page dimensions scaled into the first page format in Word.')
        para=word.add_paragraph()
        para.paragraph_format.space_before=Pt(0);para.paragraph_format.space_after=Pt(0)
        para.paragraph_format.line_spacing=Pt(1)
        # Anchored shapes use PAGE coordinates, so one minimal paragraph per page.
        raw=page.get_text('dict')
        lines=[]
        for block in raw.get('blocks',[]):
            if block.get('type')!=0:continue
            for ln in block.get('lines',[]):
                spans=[s for s in ln.get('spans',[]) if s.get('text','').strip()]
                if not spans:continue
                full=''.join(str(s.get('text','')) for s in spans).strip()
                if not full: continue
                primary=max(spans,key=lambda s: len(s.get('text','').strip()))
                lines.append({'text':full,'rect':ln.get('bbox', primary['bbox']),
                              'font_size':primary.get('size',10),'font':primary.get('font',''),
                              'flags':primary.get('flags',0),'color':primary.get('color',0)})
        # Produce a transparent-to-editors background of pictures, borders, and logos.
        # Remove text only; redraw it as editable Word textboxes above the artwork.
        work=fitz.open();work.insert_pdf(document,from_page=index,to_page=index)
        background=work[0]
        for line in lines:
            bbox=fitz.Rect(line['rect'])
            background.add_redact_annot(bbox,fill=None,cross_out=False)
        if lines:
            background.apply_redactions(images=0,graphics=0,text=0)
        zoom = max(1.05, min(1.7, dpi/72))
        image=background.get_pixmap(matrix=fitz.Matrix(zoom,zoom),alpha=False).tobytes('png')
        work.close()
        picture=para.add_run()
        picture.add_picture(io.BytesIO(image),width=Pt(first.width),height=Pt(first.height))
        _set_background_anchor(picture, first.width, first.height)
        for line in lines:
            line_id+=1
            _text_box(para,line,line_id,float(first.width),float(first.height))
        if index+1<len(document):
            # A page break inserted into the same small anchor paragraph prevents
            # the large empty paragraph after a drawing from creating spare pages.
            para.add_run().add_break(WD_BREAK.PAGE)
    document.close()
    output=io.BytesIO();word.save(output)
    warnings.append('Word contains editable positioned text and page artwork. Word/LibreOffice may substitute unavailable fonts or slightly reposition lines; PDF remains the pixel-accurate reference.')
    return output.getvalue(), warnings
