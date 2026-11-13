import fitz
import pytest
from app.pdf_engine import inspect_pdf, render_replacement_pdf


def make_pdf(with_text=True):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(35, 35, 560, 807), color=(0.65, 0.60, 0.82), width=1.2)
    page.draw_circle((520, 70), 15, color=(0.85, 0.65, 0.58), fill=(0.85, 0.65, 0.58))
    if with_text:
        page.insert_text((70, 100), "PROJECT REPORT", fontsize=20, fontname="hebo")
        page.insert_textbox(fitz.Rect(70, 150, 500, 230), "Existing abstract body text used only to test layout extraction.", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_text_reconstruction_preserves_page_and_artwork():
    source = make_pdf()
    doc, template = inspect_pdf(source)
    doc.close()
    plan = {"pages": [{"page_index": 0, "blocks": [
        {"block_id": 0, "text": "LUMIDESK PROJECT REPORT"},
        {"block_id": 1, "text": "This is new, original content for a student project report."},
    ]}]}
    output, warnings = render_replacement_pdf(source, template, plan)
    rebuilt = fitz.open(stream=output, filetype="pdf")
    assert len(rebuilt) == 1
    text = rebuilt[0].get_text()
    assert "LUMIDESK PROJECT REPORT" in text
    assert "Existing abstract body text" not in text
    assert "new, original content" in text
    assert warnings == []
    rebuilt.close()


def test_scanned_pdf_without_text_is_rejected():
    with pytest.raises(ValueError, match="No selectable text"):
        inspect_pdf(make_pdf(with_text=False))
