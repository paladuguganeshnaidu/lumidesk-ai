from __future__ import annotations

import os
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

from .ai import AIUnavailable, report_plan, tutor_response
from .db import ReportRecord, UsageEvent, User, daily_count, get_db, init_db, record_usage, upsert_user
from .pdf_engine import inspect_pdf, render_replacement_pdf
from .security import csrf_token, current_user, require_admin, require_csrf

BASE_DIR = Path(__file__).resolve().parent
APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PROD = APP_ENV == "production"
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-this-secret-before-deploy")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "15"))
TUTOR_LIMIT = int(os.getenv("DAILY_TUTOR_LIMIT", "35"))
REPORT_LIMIT = int(os.getenv("DAILY_REPORT_LIMIT", "3"))

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
    if used >= TUTOR_LIMIT:
        raise HTTPException(status_code=429, detail=f"You've reached today's learning limit ({TUTOR_LIMIT}). Please try again in 24 hours.")
    try:
        result = await tutor_response(payload.message, payload.level, payload.style)
    except AIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="The AI service did not return a usable answer. Please retry in a moment.") from exc
    record_usage(db, user["id"], "tutor", payload.message[:100])
    return {"result": result, "usage": {"used": used + 1, "limit": TUTOR_LIMIT}}


@app.post("/api/reports/generate")
async def api_generate_report(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(..., max_length=180),
    topic: str = Form(..., max_length=3000),
    student_name: str = Form(..., max_length=140),
    institution: str = Form("", max_length=180),
    department: str = Form("", max_length=180),
    guide: str = Form("", max_length=140),
    instructions: str = Form("", max_length=3000),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    user = current_user(request)
    used = daily_count(db, user["id"], "report")
    if used >= REPORT_LIMIT:
        raise HTTPException(status_code=429, detail=f"You've reached today's report limit ({REPORT_LIMIT}).")
    if file.content_type != "application/pdf" and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Upload a PDF reference template.")
    source = await file.read((MAX_UPLOAD_MB * 1024 * 1024) + 1)
    if len(source) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"PDF exceeds the {MAX_UPLOAD_MB} MB upload limit.")
    try:
        doc, template = inspect_pdf(source)
        doc.close()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Remove original body text before sending structural data to the model.
    structural_pages = []
    for page in template["pages"]:
        structural_blocks = []
        for block in page["blocks"]:
            source_label = block["text"].strip()
            lowered = source_label.lower()
            normalized_heading = re.sub(r"[^a-z0-9 ]", "", lowered).strip()
            generic_headings = {
                "abstract", "acknowledgement", "acknowledgment", "declaration", "certificate",
                "introduction", "problem statement", "objectives", "scope", "literature review",
                "related work", "methodology", "system design", "architecture", "implementation",
                "results", "discussion", "conclusion", "future work", "references", "bibliography",
                "appendix", "annexure", "contents", "table of contents",
            }
            is_heading = len(source_label) <= 90 and (source_label.isupper() or float(block["font_size"]) >= 12.5)
            if any(word in lowered for word in ("signature", "approved", "certified", "certificate", "principal", "examiner")):
                role = "approval_or_signature_placeholder"
                label = "Authorized approval/signature area"
            elif is_heading and normalized_heading in generic_headings:
                role = "heading"
                label = normalized_heading.title()
            elif is_heading and page["page_index"] == 0:
                role = "cover_title"
                label = "New project title field"
            elif is_heading:
                role = "section_heading"
                label = "Section heading"
            elif len(source_label) <= 55 and page["page_index"] == 0:
                role = "cover_metadata"
                label = "Student/institution metadata field"
            else:
                role = "body_text"
                label = "Original body text omitted; write new content for the requested project."
            structural_blocks.append({
                "block_id": block["block_id"],
                "role": role,
                "structural_label": label,
                "rect": block["rect"],
                "font_size": block["font_size"],
                "character_budget": min(500, max(28, len(source_label))),
            })
        structural_pages.append({
            "page_index": page["page_index"],
            "width": page["width"], "height": page["height"],
            "image_count": page["image_count"],
            "blocks": structural_blocks,
        })
    try:
        plan = await report_plan(topic=topic, title=title, student=student_name, institution=institution, department=department, guide=guide, instructions=instructions, template={"page_count": template["page_count"], "pages": structural_pages})
        output, warnings = render_replacement_pdf(source, template, plan)
    except AIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="The report agent could not complete this template. Try a simpler text-based PDF.") from exc
    record = ReportRecord(user_id=user["id"], title=title, pages=template["page_count"], status="completed")
    db.add(record)
    record_usage(db, user["id"], "report", title)
    db.commit()
    safe_name = "".join(c for c in title if c.isalnum() or c in " -_").strip().replace(" ", "_")[:50] or "Student_Report"
    import urllib.parse, json
    headers = {"Content-Disposition": f'attachment; filename="{safe_name}_LumiDesk.pdf"', "X-Report-Warnings": str(len(warnings)), "X-Report-Warning-Details": urllib.parse.quote(json.dumps(warnings, ensure_ascii=False))}
    # Warnings are surfaced in a response header and included in a sidecar JSON header only as a count;
    # the client receives detailed fit warnings through the response metadata endpoint in a later iteration.
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
