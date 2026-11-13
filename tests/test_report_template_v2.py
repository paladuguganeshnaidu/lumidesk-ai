import pymupdf as fitz
from app.pdf_engine import inspect_pdf, render_replacement_pdf
from app.report_pipeline import extract_template_details, content_slots, section_of, build_plan


def sample_template():
    doc = fitz.open()
    cover = doc.new_page(width=595, height=842)
    cover.insert_text((60, 60), "NAGARJUNA COLLEGE OF ENGINEERING AND TECHNOLOGY", fontsize=14)
    cover.insert_text((60, 93), "DEPARTMENT OF COMPUTER SCIENCE AND ENGINEERING", fontsize=13)
    cover.insert_text((150, 180), "Report Titled", fontsize=14)
    cover.insert_text((150, 215), '"Cyber Suraksha Abhiyan"', fontsize=15)
    cover.insert_text((150, 290), "Nandhushree M(1NC24CS133)", fontsize=13)
    chapter = doc.new_page(width=595, height=842)
    chapter.insert_text((50, 50), "National Service Scheme", fontsize=10)
    chapter.insert_text((50, 110), "Introduction", fontsize=17)
    chapter.insert_text((60, 155), "This cyber safety awareness activity is for local communities.", fontsize=12)
    chapter.insert_text((60, 176), "Students learn about phishing, safe payments and social media.", fontsize=12)
    chapter.insert_text((50, 760), "Dept of CSE,NCET | 2025-26 | 1", fontsize=10)
    chapter.draw_rect(fitz.Rect(35, 30, 560, 790), color=(0, 0, 0))
    result = doc.tobytes()
    doc.close()
    return result


def test_inspect_preserves_source_identity_and_section():
    doc, structure = inspect_pdf(sample_template())
    doc.close()
    details = extract_template_details(structure)
    assert details["source_title"] == "Cyber Suraksha Abhiyan"
    assert details["source_usn"] == "1NC24CS133"
    assert details["source_name"] == "Nandhushree M"
    assert "NAGARJUNA COLLEGE" in details["institution"]
    sections = content_slots(structure)
    assert sections[0]["section"] == "Introduction"
    assert len(sections[0]["slots"]) == 2


def test_only_selected_lines_change_and_design_survives():
    original = sample_template()
    doc, structure = inspect_pdf(original)
    doc.close()
    details = extract_template_details(structure)
    sections = content_slots(structure)
    chapter = sections[0]
    new_text = [
        "This student project introduces practical smart farming tools.",
        "It explores sensors, data visualization and crop monitoring.",
    ]
    ai_pages = [{"page_index": chapter["page_index"], "blocks": [
        {"block_id": line["block_id"], "text": value}
        for line, value in zip(chapter["slots"], new_text)
    ]}]
    plan = build_plan(structure, details, "Smart Farming Systems",
                      "Ganesh Naidu", "1NC24CS007", ai_pages)
    assert plan["rewritten_body_slots"] == plan["planned_body_slots"]
    exported, warnings = render_replacement_pdf(original, structure, plan)
    assert warnings == []
    pdf = fitz.open(stream=exported, filetype="pdf")
    assert len(pdf) == 2
    cover_text, chapter_text = pdf[0].get_text(), pdf[1].get_text()
    assert "Smart Farming Systems" in cover_text
    assert "Ganesh Naidu" in cover_text
    assert "Nandhushree" not in cover_text
    assert "NAGARJUNA COLLEGE OF ENGINEERING AND TECHNOLOGY" in cover_text
    assert "Introduction" in chapter_text
    assert "Dept of CSE,NCET" in chapter_text
    assert "smart farming" in chapter_text
    assert "cyber safety" not in chapter_text
    pdf.close()


def test_table_of_contents_is_protected():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((80, 80), "Table of Contents", fontsize=18)
    page.insert_text((80, 150), "Certificate", fontsize=13)
    page.insert_text((80, 190), "Introduction", fontsize=13)
    data = doc.tobytes()
    doc.close()
    pdf, structure = inspect_pdf(data)
    pdf.close()
    assert section_of(structure["pages"][0]) == "table of contents"
    assert content_slots(structure) == []
