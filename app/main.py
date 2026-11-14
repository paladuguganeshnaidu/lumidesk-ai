from __future__ import annotations

import os
import re
import asyncio
import logging
import secrets
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from authlib.integrations.starlette_client import OAuth
except ImportError:  # Allows local health checks when optional OAuth dependencies are not installed.
    OAuth = None
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from .ai import AIUnavailable, rewrite_report_pages, tutor_response
from .db import ReportRecord, UsageEvent, User, daily_count, get_db, init_db, record_usage, upsert_user
from .pdf_engine import inspect_pdf, render_replacement_pdf
from .pdf_quality import verify_pdf_layout
from .typography import summarize_typography
from .report_pipeline import extract_template_details, content_slots, build_plan, extract_approved_reference_context
from .report_jobs import create_job, for_user, public_job
from .image_editor import MAX_IMAGE_BYTES, image_slots, normalize_upload, apply_replacements, slot_preview
from .docx_export import export_editable_docx
from .security import csrf_token, current_user, require_admin, require_csrf
from .billing import price_info as billing_price_info, create_checkout, create_customer_portal, can_manage_subscription, handle_webhook, history as billing_history

BASE_DIR = Path(__file__).resolve().parent
APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PROD = APP_ENV == "production"
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-this-secret-before-deploy")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "30"))
TUTOR_LIMIT = max(0, int(os.getenv("DAILY_TUTOR_LIMIT", "0")))
REPORT_LIMIT = max(0, int(os.getenv("DAILY_REPORT_LIMIT", "0")))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="LumiDesk AI", version="1.1.0", docs_url=None if IS_PROD else "/api/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="lumidesk_session", same_site="lax", https_only=IS_PROD, max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


oauth = OAuth() if OAuth else None
if oauth and os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"):
    oauth.register(
        name="google",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self' https://unpkg.com https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: https:; connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com https://integrate.api.nvidia.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://accounts.google.com; object-src 'none'")
    response.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith(("/api", "/auth")) else "public, max-age=300")
    if IS_PROD:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/health")
def health():
    return {"status": "ok", "service": "lumidesk-ai", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/system/readiness")
def system_readiness():
    """Non-secret integration readiness, not a paid inference or quality claim."""
    return {
        "service": "lumidesk-ai",
        "ai_key_configured": bool(os.getenv("NVIDIA_API_KEY", "").strip()),
        "ai_model_configured": bool(os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b").strip()),
        "pdf_max_pages": 70,
        "upload_limit_mb": MAX_UPLOAD_MB,
        "visual_pdf_qa_enabled": True,
        "note": "Configured means a key is present; it does not verify provider quota or authorization."
    }


@app.head("/health")
def health_head():
    return Response(status_code=200)


@app.head("/")
def home_head():
    return Response(status_code=200)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = request.session.get("user")
    return templates.TemplateResponse(request, "index.html", {"user": user, "csrf": csrf_token(request)})


@app.get("/app", response_class=HTMLResponse)
def workspace(request: Request):
    user = request.session.get("user")
    return templates.TemplateResponse(request, "workspace.html", {"user": user, "csrf": csrf_token(request)})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = request.session.get("user")
    return templates.TemplateResponse(request, "admin.html", {"user": user, "csrf": csrf_token(request)})


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse(request, "legal.html", {"request": request, "page_title": "Privacy", "kind": "privacy"})


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse(request, "legal.html", {"request": request, "page_title": "Terms", "kind": "terms"})


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    return templates.TemplateResponse(request, "billing.html", {
        "request": request, "user": request.session.get("user"), "csrf": csrf_token(request),
    })


@app.get("/api/billing/config")
def billing_config():
    # An unavailable gateway must not leak account keys or internal secrets.
    return billing_price_info()


@app.get("/api/billing/history")
def billing_payments(request: Request):
    user = current_user(request)
    return {"payments": billing_history(int(user["id"])), "can_manage_subscription": can_manage_subscription(int(user["id"]))}


@app.post("/api/billing/checkout")
async def billing_checkout(request: Request):
    require_csrf(request)
    user = current_user(request)
    return await asyncio.to_thread(create_checkout, user)


@app.post("/api/billing/portal")
async def billing_portal(request: Request):
    require_csrf(request)
    user = current_user(request)
    return await asyncio.to_thread(create_customer_portal, int(user["id"]))


@app.post("/api/billing/webhook")
async def billing_stripe_webhook(request: Request):
    # Stripe sends server-to-server signed events, NOT browser CSRF tokens.
    # Always verify the raw request bytes before changing an order.
    raw = await request.body()
    signature = request.headers.get("stripe-signature", "")
    return await asyncio.to_thread(handle_webhook, raw, signature)


@app.get("/auth/google")
async def google_login(request: Request):
    if not (oauth and os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")):
        return templates.TemplateResponse(request, "setup.html", {"request": request}, status_code=503)
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI") or str(request.url_for("google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback", name="google_callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    if not (oauth and os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")):
        raise HTTPException(status_code=503, detail="Google sign-in is not configured.")
    try:
        token = await oauth.google.authorize_access_token(request)
        info = token.get("userinfo") or await oauth.google.parse_id_token(request, token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Google sign-in could not be verified. Please try again.") from exc
    if not info or not info.get("sub") or not info.get("email") or not info.get("email_verified"):
        raise HTTPException(status_code=401, detail="Google did not return a verified email address.")
    user = upsert_user(db, sub=info["sub"], email=info["email"], name=info.get("name") or info["email"].split("@")[0], picture=info.get("picture"))
    request.session.clear()
    request.session["user"] = {"id": user.id, "email": user.email, "name": user.name, "picture": user.picture, "role": user.role, "active": user.active}
    csrf_token(request)
    return RedirectResponse(url="/admin" if user.role == "admin" else "/app", status_code=303)


@app.post("/auth/logout")
async def logout(request: Request):
    require_csrf(request)
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    user = request.session.get("user")
    return {"authenticated": bool(user), "user": user, "csrf": csrf_token(request), "limits": {"tutor_daily": TUTOR_LIMIT, "reports_daily": REPORT_LIMIT}}


class TutorInput(BaseModel):
    message: str = Field(min_length=3, max_length=4000)
    level: str = Field(default="beginner", max_length=40)
    style: str = Field(default="visual", max_length=40)


@app.post("/api/tutor")
async def api_tutor(payload: TutorInput, request: Request, db: Session = Depends(get_db)):
    require_csrf(request)
    user = current_user(request)
    used = daily_count(db, user["id"], "tutor")
    if TUTOR_LIMIT > 0 and used >= TUTOR_LIMIT:
        raise HTTPException(status_code=429, detail=f"You've reached today's learning limit ({TUTOR_LIMIT}). Please try again in 24 hours.")
    try:
        result = await tutor_response(payload.message, payload.level, payload.style)
    except AIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="The AI service did not return a usable answer. Please retry in a moment.") from exc
    record_usage(db, user["id"], "tutor", payload.message[:100])
    return {"result": result, "usage": {"used": used + 1, "limit": TUTOR_LIMIT}}


async def _read_report_upload(file: UploadFile) -> bytes:
    if file.content_type != "application/pdf" and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Please upload a PDF reference document.")
    source = await file.read((MAX_UPLOAD_MB * 1024 * 1024) + 1)
    if len(source) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"The selected PDF is larger than {MAX_UPLOAD_MB} MB.")
    # ISO-compliant PDFs may contain a short binary preamble before the header.
    if b"%PDF-" not in source[:1024]:
        raise HTTPException(status_code=422, detail="This is not a readable PDF document.")
    return source


def _read_template(source: bytes) -> dict:
    try:
        document, template = inspect_pdf(source)
        document.close()
        return template
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("PDF template analysis failed")
        raise HTTPException(status_code=422, detail="The PDF could not be analyzed. Try an unlocked, text-based PDF.") from exc


@app.post("/api/reports/analyze")
async def analyze_report_template(request: Request, file: UploadFile = File(...)):
    require_csrf(request)
    current_user(request)
    source = await _read_report_upload(file)
    template = await asyncio.to_thread(_read_template, source)
    analysis = extract_template_details(template)
    slots = content_slots(template)
    analysis["typography"] = summarize_typography(template)
    analysis["editable_sections"] = [{"page": s["page_index"]+1, "name": s["section"], "lines": len(s["slots"])} for s in slots]
    analysis["editable_line_count"] = sum(len(s["slots"]) for s in slots)
    analysis["note"] = "Only body content and requested student/title details will be changed. Headings, college details, formatting and pictures are retained."
    return analysis


@app.post("/api/reports/start", status_code=202)
async def start_report_job(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(..., min_length=4, max_length=180),
    topic: str = Form(..., min_length=20, max_length=3000),
    student_name: str = Form(..., min_length=2, max_length=140),
    usn: str = Form(..., min_length=2, max_length=32),
    db: Session = Depends(get_db),
):
    """Queue long, bounded generation without holding an HTTP request open."""
    require_csrf(request)
    user = current_user(request)
    if REPORT_LIMIT > 0 and daily_count(db, user["id"], "report") >= REPORT_LIMIT:
        raise HTTPException(status_code=429, detail="Today's report allowance has been reached.")
    source = await _read_report_upload(file)
    # Validate the file before returning a job ID; expensive work continues later.
    template = await asyncio.to_thread(_read_template, source)
    if not 1 <= template["page_count"] <= 70:
        raise HTTPException(status_code=422, detail="Upload a PDF between 1 and 70 pages.")
    try:
        job = create_job(
            user_id=user["id"], source=source, title=title.strip(), topic=topic.strip(),
            student_name=student_name.strip(), usn=usn.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return public_job(job)


@app.post("/api/reports/approved/start", status_code=202)
async def start_approved_reference_job(
    request: Request,
    file: UploadFile = File(...),
    student_name: str = Form(..., min_length=2, max_length=140),
    usn: str = Form(..., min_length=2, max_length=32),
    permission_granted: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Generate an original draft using a separately authorized, same-topic PDF format.

    This does not certify originality, endorse copying someone else's work,
    or assert that referenced activities were carried out by the new student.
    """
    require_csrf(request)
    user = current_user(request)
    if not permission_granted:
        raise HTTPException(status_code=422, detail='Confirm you have permission to use this PDF as a reference.')
    if REPORT_LIMIT > 0 and daily_count(db, user['id'], 'report') >= REPORT_LIMIT:
        raise HTTPException(status_code=429, detail='The daily report allowance has been reached.')
    source = await _read_report_upload(file)
    template = await asyncio.to_thread(_read_template, source)
    analysis = extract_template_details(template)
    if not content_slots(template):
        raise HTTPException(status_code=422, detail='The approved PDF has no identifiable editable body sections. Use a text-based report with section headings.')
    title, topic = extract_approved_reference_context(template, analysis)
    try:
        job = create_job(user_id=user['id'], source=source, title=title, topic=topic,
                         student_name=student_name.strip(), usn=usn.strip(),
                         mode='approved_reference')
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return public_job(job)


@app.get("/api/reports/jobs/{job_id}")
def report_job_status(job_id: str, request: Request):
    user = current_user(request)
    job = for_user(job_id, user["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found or it has expired.")
    return public_job(job)


@app.get("/api/reports/jobs/{job_id}/quality")
def report_quality_details(job_id: str, request: Request):
    user = current_user(request)
    job = for_user(job_id, user["id"])
    if not job:
        raise HTTPException(status_code=404, detail="Report not found or expired.")
    if job.status != "completed" or not job.quality:
        raise HTTPException(status_code=409, detail="Visual checks are not finished.")
    return job.quality


@app.get("/api/reports/jobs/{job_id}/preview")
def report_comparison_preview(job_id: str, request: Request, page: int = 1, kind: str = "generated"):
    """Protected side-by-side inspection. No generated page is made public."""
    import pymupdf
    user = current_user(request)
    job = for_user(job_id, user["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Report not found or expired.")
    if job.status != "completed" or not job.result:
        raise HTTPException(status_code=409, detail="Finish the report before viewing page previews.")
    if kind not in ("source", "generated"):
        raise HTTPException(status_code=422, detail="Preview the original or generated page.")
    pdf_bytes = job.source if kind == "source" else job.result
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if page < 1 or page > len(doc):
            raise HTTPException(status_code=422, detail="Page number is outside this report.")
        pix = doc[page-1].get_pixmap(matrix=pymupdf.Matrix(1.15, 1.15), alpha=False)
        data = pix.tobytes("png")
    return Response(content=data, media_type="image/png", headers={"Cache-Control":"private, no-store"})


@app.get("/api/reports/jobs/{job_id}/download")
def download_report_job(job_id: str, request: Request, format: str = "pdf"):
    user = current_user(request)
    job = for_user(job_id, user["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found or it has expired.")
    if format not in ("pdf", "docx"):
        raise HTTPException(status_code=422, detail="Choose PDF or Word (.docx).")
    if job.status != "completed" or not job.result:
        raise HTTPException(status_code=409, detail="The report has not finished generating.")
    content = job.result if format == "pdf" else job.word_result
    if not content:
        raise HTTPException(status_code=409, detail="The Word export is not available for this report. Download the PDF instead.")
    safe_name = "".join(c for c in job.title if c.isalnum() or c in " -_").strip().replace(" ", "_")[:55] or "LumiDesk_Report"
    return Response(
        content=content,
        media_type="application/pdf" if format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_LumiDesk.{format}"',
            "Cache-Control": "private, no-store",
            "X-Report-Warnings": str(len(job.warnings)),
        },
    )


@app.get('/api/reports/jobs/{job_id}/images')
def report_image_list(job_id: str, request: Request):
    """Discover each individual image placement after a report is generated."""
    user = current_user(request)
    job = for_user(job_id, user['id'])
    if not job:
        raise HTTPException(status_code=404, detail='Report not found or expired.')
    if job.status != 'completed' or not job.result:
        raise HTTPException(status_code=409, detail='Generate the report before editing its images.')
    base = job.base_result or job.result
    images = image_slots(base)
    for slot in images:
        slot['edited'] = slot['id'] in job.image_edits
        slot['preview_url'] = f'/api/reports/jobs/{job.id}/images/{slot["id"]}/preview'
    return {'images': images, 'count': len(images), 'edited_count': len(job.image_edits),
            'busy': job.edit_busy, 'temporary': True}


@app.get('/api/reports/jobs/{job_id}/images/{slot_id}/preview')
def report_image_preview(job_id: str, slot_id: str, request: Request):
    user = current_user(request)
    job = for_user(job_id, user['id'])
    if not job or not job.result or job.status != 'completed':
        raise HTTPException(status_code=404, detail='Report not found or expired.')
    slot = next((s for s in image_slots(job.base_result or job.result) if s['id'] == slot_id), None)
    if slot is None:
        raise HTTPException(status_code=404, detail='Image placement not found.')
    return Response(content=slot_preview(job.result, slot), media_type='image/png',
                    headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


async def _edit_report_image(job, slot_id: str, image_data: bytes | None):
    """Keep both export formats in sync. If anything fails, retain the last valid downloads."""
    if job.status != 'completed' or not job.result:
        raise HTTPException(status_code=409, detail='Generate the report before editing images.')
    if job.edit_busy:
        raise HTTPException(status_code=409, detail='Another image edit is being saved. Try again.')
    base = job.base_result or job.result
    if slot_id not in {s['id'] for s in image_slots(base)}:
        raise HTTPException(status_code=404, detail='Image placement not found. Refresh the editor.')
    changes = dict(job.image_edits)
    if image_data is None:
        if slot_id not in changes:
            return public_job(job)
        changes.pop(slot_id)
    else:
        if len(changes) >= 100 and slot_id not in changes:
            raise HTTPException(status_code=422, detail='This report already has 100 image replacements.')
        try:
            changes[slot_id] = normalize_upload(image_data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    job.edit_busy = True
    try:
        new_pdf = await asyncio.to_thread(apply_replacements, base, changes)
        try:
            new_docx, word_warnings = await asyncio.to_thread(export_editable_docx, new_pdf)
        except Exception:
            logging.exception('Word export after image replacement failed: %s', job.id)
            new_docx = None  # never return an outdated Word file beside the new PDF
        # Atomic swap only after successfully generating a verified PDF.
        job.base_result = base
        job.image_edits = changes
        job.result = new_pdf
        job.word_result = new_docx
        job.detail = 'Image edits saved. Download the updated PDF and Word file.' if new_docx else 'Edited PDF ready. Word export was unavailable.'
        if changes and not any('Replaced photos must' in w for w in job.warnings):
            job.warnings.append('Replaced photos must show your actual work. Review image captions and evidence before submission.')
        return public_job(job)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception('Report image edit failed: %s', job.id)
        raise HTTPException(status_code=502, detail='Could not save the selected image. Original downloads are unchanged.') from exc
    finally:
        job.edit_busy = False


@app.post('/api/reports/jobs/{job_id}/images/{slot_id}/replace')
async def replace_report_image(job_id: str, slot_id: str, request: Request, file: UploadFile = File(...)):
    require_csrf(request)
    user = current_user(request)
    job = for_user(job_id, user['id'])
    if not job:
        raise HTTPException(status_code=404, detail='Report not found or expired.')
    payload = await file.read(MAX_IMAGE_BYTES + 1)
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail='Image must be smaller than 8 MB.')
    return await _edit_report_image(job, slot_id, payload)


@app.post('/api/reports/jobs/{job_id}/images/{slot_id}/restore')
async def restore_report_image(job_id: str, slot_id: str, request: Request):
    require_csrf(request)
    user = current_user(request)
    job = for_user(job_id, user['id'])
    if not job:
        raise HTTPException(status_code=404, detail='Report not found or expired.')
    return await _edit_report_image(job, slot_id, None)


@app.post("/api/reports/jobs/{job_id}/regenerate", status_code=202)
async def regenerate_report_job(
    job_id: str,
    request: Request,
    student_name: str = Form(..., min_length=2, max_length=140),
    usn: str = Form(..., min_length=2, max_length=32),
    db: Session = Depends(get_db),
):
    """Start a distinct body-text variant from the untouched ORIGINAL PDF."""
    require_csrf(request)
    user = current_user(request)
    parent = for_user(job_id, user["id"])
    if parent is None:
        raise HTTPException(status_code=404, detail="Original report expired; upload the source PDF again.")
    if parent.status != "completed":
        raise HTTPException(status_code=409, detail="Finish the current report before regenerating.")
    if REPORT_LIMIT > 0 and daily_count(db, user["id"], "report") >= REPORT_LIMIT:
        raise HTTPException(status_code=429, detail="Today's report allowance has been reached.")
    try:
        job = create_job(
            user_id=user["id"], source=parent.source, title=parent.title,
            topic=parent.topic, student_name=student_name.strip(), usn=usn.strip(),
            parent=parent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return public_job(job)


@app.post("/api/reports/generate")
async def api_generate_report(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(..., min_length=4, max_length=180),
    topic: str = Form(..., min_length=20, max_length=3000),
    student_name: str = Form(..., min_length=2, max_length=140),
    usn: str = Form(..., min_length=2, max_length=32),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    user = current_user(request)
    used = daily_count(db, user["id"], "report")
    if REPORT_LIMIT > 0 and used >= REPORT_LIMIT:
        raise HTTPException(status_code=429, detail="You have reached the configured daily report limit.")
    title, topic, student_name, usn = title.strip(), topic.strip(), student_name.strip(), usn.strip()
    if not all((title, topic, student_name, usn)):
        raise HTTPException(status_code=422, detail="Enter your project title, what it does, your name, and your USN or registration number.")
    source = await _read_report_upload(file)
    template = await asyncio.to_thread(_read_template, source)
    analysis = extract_template_details(template)
    sections = content_slots(template)
    if not sections:
        raise HTTPException(status_code=422, detail="No editable body text was identified. Use a searchable PDF with several body paragraphs, or convert this scan to a selectable-text PDF.")
    try:
        # Chunked section-level writing uses bounded retries, not one enormous
        # prompt that can truncate and leave the document half rewritten.
        generated_pages = await asyncio.wait_for(rewrite_report_pages(title=title, topic=topic, sections=sections), timeout=155)
        plan = build_plan(template, analysis, title, student_name, usn, generated_pages, preserve_references=True)
        if plan["rewritten_body_slots"] != plan["planned_body_slots"] or plan["rejected_ai_slots"]:
            raise ValueError("A report section was incomplete or exceeded its original line lengths. Please retry with a clearer topic description.")
        output, fit_warnings = await asyncio.to_thread(render_replacement_pdf, source, template, plan)
        quality = await asyncio.to_thread(verify_pdf_layout, source, output, template, plan)
    except AIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="The report needs more generation time than this free server allows. Try a shorter template or retry.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("Template-aware report generation failed")
        raise HTTPException(status_code=502, detail="The report could not be completed and verified. No incomplete PDF was returned; please retry.") from exc
    record = ReportRecord(user_id=user["id"], title=title, pages=template["page_count"], status="completed")
    db.add(record)
    record_usage(db, user["id"], "report", title)
    db.commit()
    safe_name = "".join(c for c in title if c.isalnum() or c in " -_").strip().replace(" ", "_")[:50] or "Student_Report"
    import urllib.parse, json
    warnings = analysis["warnings"] + ["Original references remain visible but must be verified or replaced for the new project. Draft certification requires authorized approval."] + fit_warnings
    headers = {"Content-Disposition": f'attachment; filename="{safe_name}_LumiDesk.pdf"',
               "X-Report-Warnings": str(len(warnings)),
               "X-Visual-QA": quality["status"],
               "X-Report-Warning-Details": urllib.parse.quote(json.dumps(warnings[:12], ensure_ascii=False))}
    return Response(content=output, media_type="application/pdf", headers=headers)


@app.get("/api/student/overview")
def student_overview(request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    recent = db.scalars(select(ReportRecord).where(ReportRecord.user_id == user["id"]).order_by(ReportRecord.created_at.desc()).limit(8)).all()
    return {
        "user": user,
        "usage": {"tutor": daily_count(db, user["id"], "tutor"), "reports": daily_count(db, user["id"], "report"), "tutor_limit": TUTOR_LIMIT, "report_limit": REPORT_LIMIT},
        "reports": [{"title": r.title, "pages": r.pages, "created_at": r.created_at.isoformat()} for r in recent],
    }


@app.get("/api/admin/overview")
def admin_overview(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    users = int(db.scalar(select(func.count(User.id))) or 0)
    active = int(db.scalar(select(func.count(User.id)).where(User.last_login >= since)) or 0)
    reports = int(db.scalar(select(func.count(ReportRecord.id))) or 0)
    tutor_calls = int(db.scalar(select(func.count(UsageEvent.id)).where(UsageEvent.kind == "tutor", UsageEvent.created_at >= since)) or 0)
    report_calls = int(db.scalar(select(func.count(UsageEvent.id)).where(UsageEvent.kind == "report", UsageEvent.created_at >= since)) or 0)
    latest_users = db.scalars(select(User).order_by(User.created_at.desc()).limit(12)).all()
    latest_reports = db.scalars(select(ReportRecord).order_by(ReportRecord.created_at.desc()).limit(12)).all()
    return {
        "metrics": {"users": users, "active_7d": active, "reports": reports, "tutor_calls_7d": tutor_calls, "report_calls_7d": report_calls},
        "users": [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "active": u.active, "created_at": u.created_at.isoformat()} for u in latest_users],
        "reports": [{"title": r.title, "pages": r.pages, "user_id": r.user_id, "created_at": r.created_at.isoformat()} for r in latest_reports],
        "system": {"ai_configured": bool(os.getenv("NVIDIA_API_KEY")), "google_configured": bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")), "database": "postgresql" if "postgresql" in str(db.bind.url) else "sqlite (ephemeral on Render Free)"},
    }


@app.post("/api/admin/users/{user_id}/toggle")
def admin_toggle_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request)
    require_csrf(request)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.id == admin["id"]:
        raise HTTPException(status_code=400, detail="You cannot disable your own admin account.")
    user.active = not user.active
    db.commit()
    return {"ok": True, "active": user.active}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
