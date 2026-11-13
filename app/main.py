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
from .report_pipeline import extract_template_details, content_slots, build_plan
from .report_jobs import create_job, for_user, public_job
from .security import csrf_token, current_user, require_admin, require_csrf

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


app = FastAPI(title="LumiDesk AI", version="1.0.0", docs_url=None if IS_PROD else "/api/docs", redoc_url=None, lifespan=lifespan)
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
    if not source.startswith(b"%PDF-"):
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
    template = _read_template(source)
    analysis = extract_template_details(template)
    slots = content_slots(template)
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


@app.get("/api/reports/jobs/{job_id}")
def report_job_status(job_id: str, request: Request):
    user = current_user(request)
    job = for_user(job_id, user["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found or it has expired.")
    return public_job(job)


@app.get("/api/reports/jobs/{job_id}/download")
def download_report_job(job_id: str, request: Request):
    user = current_user(request)
    job = for_user(job_id, user["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found or it has expired.")
    if job.status != "completed" or not job.result:
        raise HTTPException(status_code=409, detail="The report has not finished generating.")
    safe_name = "".join(c for c in job.title if c.isalnum() or c in " -_").strip().replace(" ", "_")[:55] or "LumiDesk_Report"
    return Response(
        content=job.result, media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_LumiDesk.pdf"',
            "Cache-Control": "private, no-store",
            "X-Report-Warnings": str(len(job.warnings)),
        },
    )


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
    template = _read_template(source)
    analysis = extract_template_details(template)
    sections = content_slots(template)
    if not sections:
        raise HTTPException(status_code=422, detail="This PDF has no clearly identifiable body sections to rewrite. Try a text-based report with section headings.")
    try:
        # Chunked section-level writing uses bounded retries, not one enormous
        # prompt that can truncate and leave the document half rewritten.
        generated_pages = await asyncio.wait_for(rewrite_report_pages(title=title, topic=topic, sections=sections), timeout=155)
        plan = build_plan(template, analysis, title, student_name, usn, generated_pages)
        if plan["rewritten_body_slots"] != plan["planned_body_slots"] or plan["rejected_ai_slots"]:
            raise ValueError("A report section was incomplete or exceeded its original line lengths. Please retry with a clearer topic description.")
        output, fit_warnings = render_replacement_pdf(source, template, plan)
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
    warnings = analysis["warnings"] + ["Any original references must be replaced with verified sources for the new topic; draft certification and declarations require authorization."] + fit_warnings
    headers = {"Content-Disposition": f'attachment; filename="{safe_name}_LumiDesk.pdf"',
               "X-Report-Warnings": str(len(warnings)),
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
