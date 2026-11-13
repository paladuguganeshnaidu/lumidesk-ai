from app.report_pipeline import static_replacements

def test_reference_page_preserves_visible_verification_slots():
    """Old report citations must be replaced with visible verification prompts, never blanked."""
    template = {"pages": [{"page_index": 0, "width": 595, "height": 842, "image_count": 0, "blocks": [
        {"block_id": 0, "text": "References", "rect": [70, 75, 180, 99]},
        {"block_id": 1, "text": "1. Original research reference", "rect": [60, 136, 425, 154]},
        {"block_id": 2, "text": "Second line of an old reference that may not apply", "rect": [60, 158, 425, 177]},
        {"block_id": 3, "text": "2. Another citation from previous project", "rect": [60, 188, 425, 205]},
    ]}]}
    updates = static_replacements(template, {"source_title": "", "source_name": "", "source_usn": ""}, "New Project", "Student", "1NC24CS133")
    changed = [value for (page, block), value in updates.items() if page == 0 and block in (1,2,3)]
    assert len(changed) == 3
    assert all(text.strip() and "verify" in text.lower() or "verified" in text.lower() for text in changed)
    assert all(len(text) < 100 for text in changed)
