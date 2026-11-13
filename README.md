# LumiDesk AI

A student-first AI workspace with a visual learning assistant, original academic report studio, Google OpenID Connect, and a protected admin console.

The application source is stored in the `lumidesk_source.part*.b64` segments. Render's `build.sh` joins and decodes those segments, extracts the project and installs dependencies. The full source is available in the downloadable project archive; the project README inside the source contains setup, Google OAuth, NVIDIA Nemotron, privacy, testing and deployment instructions.

**Important:** Render Free is a pilot tier with sleeping services and ephemeral local storage. Configure a durable PostgreSQL database before institutional use. Google OAuth, NVIDIA API and admin email settings must be added in Render Environment settings; no secrets are committed.
