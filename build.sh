#!/usr/bin/env bash
set -euo pipefail
python - <<'PY'
import zipfile
with zipfile.ZipFile('lumidesk_source.zip') as bundle:
    bundle.extractall('.')
PY
pip install --no-cache-dir -r requirements.txt
