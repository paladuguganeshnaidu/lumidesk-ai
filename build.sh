#!/usr/bin/env bash
set -euo pipefail
cat lumidesk_source.part*.b64 | tr -d '\n' | base64 -d > /tmp/lumidesk_source.zip
python - <<'PY'
import zipfile
with zipfile.ZipFile('/tmp/lumidesk_source.zip') as bundle:
    bundle.extractall('.')
PY
pip install --no-cache-dir -r requirements.txt
