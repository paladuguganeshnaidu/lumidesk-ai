# LumiDesk AI

A student-first AI workspace with two core experiences:

- **Visual Learning Assistant** — asks NVIDIA Nemotron for structured explanations, examples, steps, Mermaid diagrams and short knowledge checks.
- **AI Report Studio** — analyzes an authorized, text-based PDF template, generates original content for a new project, preserves the original page geometry and non-text artwork where possible, and exports a review-ready PDF.
- **Google sign-in** — OpenID Connect through Google; there are no local passwords.
- **Admin console** — allowlisted administrators can inspect basic usage, student accounts, reports and integration readiness.

The design uses a warm pastel palette, responsive bento-like cards, CSS 3D shapes, scroll reveals, purposeful micro-interactions and reduced-motion fallbacks. It is an original interface, informed by current design patterns rather than copied from another product.

## Stack

- Python 3.11+
- FastAPI + Jinja2
- SQLAlchemy (SQLite for local development; PostgreSQL through `DATABASE_URL`)
- NVIDIA Nemotron 3.5 Lightning 30B A3B through NVIDIA's OpenAI-compatible API
- Authlib / Google OpenID Connect
- PyMuPDF for PDF structure extraction and text-layer reconstruction
- Vanilla JavaScript, CSS, Lucide icons, Mermaid.js

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
# Fill the environment variables described below.
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | Yes in production | Long random secret for signed sessions |
| `GOOGLE_CLIENT_ID` | Yes for login | OAuth 2.0 web client ID |
| `GOOGLE_CLIENT_SECRET` | Yes for login | OAuth 2.0 web client secret |
| `GOOGLE_REDIRECT_URI` | Yes for deployed login | `https://YOUR-SERVICE.onrender.com/auth/callback` |
| `NVIDIA_API_KEY` | Yes for AI | NVIDIA API key from build.nvidia.com |
| `NVIDIA_BASE_URL` | No | Defaults to `https://integrate.api.nvidia.com/v1` |
| `NVIDIA_MODEL` | No | Defaults to `nvidia/nemotron-3.5-lightning-30b-a3b` |
| `ADMIN_EMAILS` | Yes for admin | Comma-separated, verified Google email allowlist |
| `DATABASE_URL` | Strongly recommended | PostgreSQL URL for durable user and activity data |
| `MAX_UPLOAD_MB` | No | PDF upload cap; defaults to 15 MB |
| `DAILY_TUTOR_LIMIT` | No | Per-account rolling 24-hour tutor cap; defaults to 35 |
| `DAILY_REPORT_LIMIT` | No | Per-account rolling 24-hour report cap; defaults to 3 |
| `APP_ENV` | Yes in production | Set to `production` to enable secure cookies and HSTS |

Never commit `.env` or API credentials. Put secrets in Render's environment settings.

## Google OAuth setup

1. In Google Cloud Console, create or select a project and configure the OAuth consent screen.
2. Create an OAuth Client ID of type **Web application**.
3. Add your deployed origin to **Authorized JavaScript origins**, for example `https://YOUR-SERVICE.onrender.com`.
4. Add `https://YOUR-SERVICE.onrender.com/auth/callback` to **Authorized redirect URIs**.
5. Add the client ID and client secret to Render as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
6. Set `GOOGLE_REDIRECT_URI` to the exact callback URI.
7. Set `ADMIN_EMAILS` to the Google email address that should administer LumiDesk.

The app checks Google's signed OIDC response and requires `email_verified=true`. Admin status is derived only from the server-side allowlist; users cannot grant themselves admin in the browser.

## NVIDIA setup

1. Sign in at [NVIDIA API Catalog](https://build.nvidia.com/).
2. Open [Nemotron 3.5 Lightning 30B A3B](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b) and generate an API key.
3. Add the key to Render as `NVIDIA_API_KEY`.
4. Keep the model ID as `nvidia/nemotron-3.5-lightning-30b-a3b`, or change `NVIDIA_MODEL` if you intentionally select another compatible model.

NVIDIA's free prototyping endpoint is quota- and availability-limited; it is not an unlimited production inference commitment. Check NVIDIA's current model page and terms before inviting a large class.

## Deploy to Render

This repository includes `render.yaml` for a free Python web service. Connect the GitHub repository to Render and create a Blueprint from the file. Then add the required secret environment variables in the Render Dashboard.

Start command:

```bash
gunicorn app.main:app -k uvicorn.workers.UvicornWorker --workers 1 --timeout 180 --bind 0.0.0.0:$PORT
```

The health endpoint is `/health`.

## Data durability and free-tier caveats

This is a **free-tier pilot deployment**, not a claim of enterprise production SLA. Render Free web services sleep after inactivity and have an ephemeral filesystem. A local SQLite database and generated local files can be lost when the service restarts, sleeps or redeploys. The app does not retain uploaded reference PDFs or generated report files; the PDF is returned to the student immediately. For persistent accounts and audit history, set `DATABASE_URL` to a durable PostgreSQL provider. Render's own free PostgreSQL offering expires after 30 days, so it is not a permanent production database. For real institutional use, add durable managed storage, backups, monitoring, rate limiting, and a privacy/security review.

## PDF report behavior and limitations

- The PDF is treated as a **design/structure reference**, not as source material to copy. The model receives layout cues, short generic headings and text-area budgets; it is instructed to write original content for the new project.
- Page size, page order, vector artwork and embedded images are preserved where possible. Text is replaced in its original bounding boxes. If Noto Sans font files are present under `app/fonts`, Latin, Devanagari and Telugu text can be embedded; otherwise the engine falls back to standard PDF fonts and warns when non-Latin characters need manual handling.
- Long content may not fit the original text area. The engine shrinks text within limits and reports areas that need manual review.
- Scanned/image-only PDFs, encrypted PDFs, files over the configured size, and templates above the page/block caps are rejected.
- Approval, certificate and signature areas are replaced with a neutral instruction for the authorized person to complete; the app does not fabricate approvals, signatures, marks or endorsements.
- Every generated report is a draft. Students must verify facts, references, calculations and institutional requirements before submission or printing.

## Security notes

- Google OIDC only; no password storage.
- Signed, HTTP-only session cookie; `Secure` in production; `SameSite=Lax`.
- CSRF token required for state-changing API calls.
- Server-side admin allowlist and role checks.
- Upload size, page-count and text-block caps.
- Uploaded PDFs are processed in memory and not stored as a file on the server.
- Security headers including CSP, HSTS (production), frame protection and MIME sniffing protection.

Before public institutional use, add automated dependency scanning, a formal privacy notice and retention policy, per-IP abuse controls, database backups, structured audit logs, and an external penetration test.

## Tests

```bash
pytest -q
```

The test suite covers page rendering, unauthenticated API rejection and PDF template reconstruction. AI calls require a real NVIDIA key and are not run in unit tests.

## Report generation v3

The Report Studio accepts text-based PDFs up to 70 pages and 30 MB and processes each section in a bounded background job. Progress and temporary downloads are available from the student workspace. After generation, **Regenerate with this name and USN** reuses the untouched original document and creates a new body-text variant, showing a local four-word overlap estimate relative to the previous version. That estimate is not a plagiarism or originality certificate. Completed PDFs and source uploads remain in single-worker process memory for up to an hour and can be lost on redeployment. If the inference provider fails, the system creates a topic-based draft fallback, which requires careful student editing and verification.
