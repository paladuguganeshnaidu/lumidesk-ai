# LumiDesk AI

A student-first AI workspace with two focused experiences:

- **Visual Learning Assistant** — NVIDIA Nemotron produces clear explanations, analogies, practical examples, step-by-step guides, Mermaid diagrams and short knowledge checks.
- **AI Report Studio** — analyzes an authorized, text-based PDF template, writes original content for a new project, preserves page geometry and non-text artwork where possible, and exports a PDF draft for review.
- **Google sign-in** — OpenID Connect; no local passwords.
- **Admin console** — server-side email allowlist, student access controls, activity counts and integration readiness.

## Stack

Python 3.11+, FastAPI, Jinja2, SQLAlchemy, Google OAuth via Authlib, NVIDIA Nemotron 3.5 Lightning 30B A3B, PyMuPDF, vanilla JavaScript/CSS, Mermaid.js and Lucide icons.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Required environment variables

- `SECRET_KEY`: long random session-signing secret.
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`: Google OAuth web client credentials.
- `GOOGLE_REDIRECT_URI`: exact callback URL, e.g. `https://lumidesk-ai.onrender.com/auth/callback`.
- `NVIDIA_API_KEY`: key from NVIDIA API Catalog.
- `ADMIN_EMAILS`: comma-separated, verified Google email addresses allowed into the admin console.
- `DATABASE_URL`: durable PostgreSQL URL for persistent accounts and activity. Without it, local SQLite is used.

Optional settings include `NVIDIA_MODEL`, `NVIDIA_BASE_URL`, `MAX_UPLOAD_MB`, `DAILY_TUTOR_LIMIT`, `DAILY_REPORT_LIMIT` and `APP_ENV`. Set `DAILY_TUTOR_LIMIT=0` and `DAILY_REPORT_LIMIT=0` for no daily application-level quotas. Provider inference quotas, costs, abuse controls and per-file upload caps still apply.

Never commit secrets. Configure them in Render Environment settings.

## Google OAuth

Create a Google OAuth client of type **Web application**. Add the site origin to Authorized JavaScript origins and the exact `/auth/callback` URL to Authorized redirect URIs. Set the client ID, secret and redirect URI in Render. Add the administrator's verified Google email to `ADMIN_EMAILS`. The server validates the OIDC identity and derives admin role only from this allowlist.

## NVIDIA Nemotron

Create an API key at [NVIDIA API Catalog](https://build.nvidia.com/) and set `NVIDIA_API_KEY`. The default model is `nvidia/nemotron-3.5-lightning-30b-a3b`. Free prototyping access has changing quotas and availability; it is not an unlimited production inference commitment.

## Render

The repository includes `render.yaml` for a free Python web service. The service runs:

```bash
gunicorn app.main:app -k uvicorn.workers.UvicornWorker --workers 1 --timeout 180 --bind 0.0.0.0:$PORT
```

Health endpoint: `/health`.

## PDF behavior and integrity

The reference PDF is used as a **layout/structure template**, not as text to copy. The model receives page geometry, text-area budgets and short structural labels; original body paragraphs are omitted. Page size, vector artwork and embedded images are preserved where possible. Text is replaced in its original bounding boxes and fit warnings are returned. Scanned/image-only, encrypted, oversized and excessively long templates are rejected. Approval, certificate and signature areas remain for authorized people to complete. Every generated PDF is a draft: students must verify facts, calculations, citations and institutional requirements before submission or printing.

## Data, privacy and free-tier limits

Uploaded PDFs and generated files are processed in memory and are not intentionally stored on the server. Student account details, usage events and report metadata are stored in the configured database. Learning questions and project instructions are sent to NVIDIA for inference; limited PDF layout cues are sent for report planning.

Render Free services sleep after inactivity and use ephemeral local storage. SQLite records may be lost during restart, sleep or redeploy. Render's free PostgreSQL offering expires after 30 days. This is a pilot/demo configuration, not an enterprise SLA. For institutional use, configure durable managed PostgreSQL, backups, retention controls, monitoring, per-IP abuse protection and a privacy/security review.

## Tests

```bash
pytest -q
```
