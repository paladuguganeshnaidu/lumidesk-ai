"""AI reliability, unfamiliar PDF templates and full-page visual QA."""
import asyncio
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import ai
from app.main import app
from app.pdf_engine import inspect_pdf, render_replacement_pdf
from app.pdf_quality import LayoutQualityError, verify_pdf_layout
from app.report_pipeline import content_slots


def make_document(pages=14, familiar=True):
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        if i == 0:
            p.insert_text((65, 80), "FICTIONAL ENGINEERING COLLEGE", fontsize=16)
            p.insert_text((65, 140), "Student report reference layout", fontsize=13)
        else:
            if familiar and i == 4:
                p.insert_text((64, 80), "Abstract", fontsize=17)
            elif i == 1:
                p.insert_text((64, 80), "Overview", fontsize=17)
            for j in range(8):
                p.insert_text((65, 165+j*25),
                              f"Students discuss a safe classroom learning workflow with documented checks and future testing {i}-{j}.",
                              fontsize=10)
    result = doc.tobytes(deflate=True)
    doc.close()
    return result


def test_custom_headings_and_seventy_page_reports_accepted():
    payload = make_document(pages=70, familiar=False)
    doc, template = inspect_pdf(payload)
    doc.close()
    assert template["page_count"] == 70
    sections = content_slots(template)
    assert len(sections) >= 60
    assert sections[0]["page_index"] == 1
    assert all(section["page_index"] != 0 for section in sections)


def test_visual_qa_detects_unapproved_content_and_allows_intended_edits():
    original = make_document(pages=14, familiar=True)
    doc, template = inspect_pdf(original)
    doc.close()
    page, line = 4, template["pages"][4]["blocks"][2]
    plan = {"pages": [{"page_index": page, "blocks": [
        {"block_id": line["block_id"], "text": "A fresh original discussion of practical learning and design review."}
    ]}]}
    output, warnings = render_replacement_pdf(original, template, plan)
    check = verify_pdf_layout(original, output, template, plan)
    assert check["status"] == "passed"
    assert check["pages_checked"] == 14
    assert check["max_outside_edit_difference_pct"] <= .5

    changed = pymupdf.open(stream=output, filetype="pdf")
    changed[5].draw_rect(pymupdf.Rect(50, 350, 400, 570), color=(1,0,0), fill=(1,0,0))
    broken = changed.tobytes(deflate=True)
    changed.close()
    with pytest.raises(LayoutQualityError):
        verify_pdf_layout(original, broken, template, plan)


def test_tutor_uses_non_thinking_nim_and_accepts_json_fences(monkeypatch):
    calls = []
    class FakeChat:
        async def create(self, **kwargs):
            calls.append(kwargs)
            fence = chr(96) * 3
            payload = fence + 'json\n{"title":"Easy explanation","explanation":"An API lets two programs communicate.","quiz":[]}\n' + fence
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])
    fake = SimpleNamespace(chat=SimpleNamespace(completions=FakeChat()))
    monkeypatch.setattr(ai, "_client", lambda: fake)
    result = asyncio.run(ai.tutor_response("Explain API like a beginner"))
    assert result["title"] == "Easy explanation"
    assert result["explanation"].startswith("An API")
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert "response_format" not in calls[0]


def test_tutor_reports_nvidia_quota_and_never_hides_empty_answer(monkeypatch):
    class ProviderError(Exception):
        status_code = 429
    class BusyChat:
        async def create(self, **kwargs):
            raise ProviderError("quota")
    fake = SimpleNamespace(chat=SimpleNamespace(completions=BusyChat()))
    monkeypatch.setattr(ai, "_client", lambda: fake)
    with pytest.raises(ai.AIUnavailable, match="quota"):
        asyncio.run(ai.tutor_response("Explain neural networks"))

    class EmptyChat:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
    fake.chat.completions = EmptyChat()
    with pytest.raises(ai.AIUnavailable, match="no final answer"):
        asyncio.run(ai.tutor_response("Explain neural networks"))


def test_analyzer_accepts_pdf_with_short_header_preamble(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "current_user", lambda _: {"id": 999, "active": True})
    monkeypatch.setattr(main, "require_csrf", lambda _: None)
    document = b"\xef\xbb\xbf" + make_document(pages=14)
    with TestClient(app) as client:
        response = client.post("/api/reports/analyze",
                               files={"file": ("report.pdf", document, "application/pdf")})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["page_count"] == 14
        assert result["editable_line_count"] > 0


def test_pdf_side_by_side_previews_are_owner_scoped(monkeypatch):
    from app import main
    from app.report_jobs import ReportJob, jobs
    source = make_document(pages=2, familiar=False)
    record = ReportJob(id="test_quality_preview", user_id=1001, source=source,
                       title="Sample", topic="Preview", student_name="Student", usn="USN",
                       status="completed", result=source, quality={"status":"passed","pages_checked":2})
    jobs[record.id] = record
    monkeypatch.setattr(main, "current_user", lambda _: {"id": 1001, "active": True})
    with TestClient(app) as client:
        source_response = client.get("/api/reports/jobs/test_quality_preview/preview?page=2&kind=source")
        output_response = client.get("/api/reports/jobs/test_quality_preview/preview?page=2&kind=generated")
        assert source_response.status_code == 200
        assert output_response.status_code == 200
        assert source_response.headers["content-type"].startswith("image/png")
        assert source_response.content.startswith(b"\x89PNG")
        assert client.get("/api/reports/jobs/test_quality_preview/preview?page=3").status_code == 422
        assert client.get("/api/reports/jobs/test_quality_preview/quality").json()["pages_checked"] == 2
        monkeypatch.setattr(main, "current_user", lambda _: {"id": 2002, "active": True})
        assert client.get("/api/reports/jobs/test_quality_preview/quality").status_code == 404
        assert client.get("/api/reports/jobs/test_quality_preview/preview").status_code == 404
    jobs.clear()
