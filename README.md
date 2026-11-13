# LumiDesk AI

A student-first AI workspace with a visual learning assistant, original academic report studio, Google OpenID Connect, and a protected admin console.

The deployable source is packaged in `lumidesk_source.zip`. Render's `build.sh` extracts it before installing dependencies. See the project README inside the archive for setup, Google OAuth, NVIDIA Nemotron, privacy, and deployment instructions.

**Important:** Render Free is a pilot tier with sleeping services and ephemeral local storage. Configure a durable PostgreSQL database before institutional use. Google OAuth and NVIDIA API credentials must be added in Render Environment settings; no secrets are committed.