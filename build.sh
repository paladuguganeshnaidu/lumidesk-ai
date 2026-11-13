#!/usr/bin/env bash
set -euo pipefail
cat lumidesk_source.part01.b64 lumidesk_source.part02.b64 lumidesk_source.part03.b64 lumidesk_source.part04.b64 lumidesk_source.part05a.b64 lumidesk_source.part05b.b64 lumidesk_source.part05c.b64 lumidesk_source.part05d.b64 | tr -d '\n' | base64 -d > /tmp/lumidesk_source.zip
python - <<'PY'
import zipfile
with zipfile.ZipFile('/tmp/lumidesk_source.zip') as bundle:
    bad = bundle.testzip()
    if bad:
        raise SystemExit(f"Corrupt source archive entry: {bad}")
    bundle.extractall('.')
PY
pip install --no-cache-dir -r requirements.txt
