"""Dump the FastAPI-generated OpenAPI schema to openapi.json (deterministic).

CI regenerates and diffs this file; a stale committed schema fails the build,
so the committed openapi.json is always the real contract. The client repo
vendors this file at a pinned version to generate its typed API client.
"""

import json
import sys
from pathlib import Path

# Make the repo root importable no matter how this script is invoked.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

out = ROOT / "openapi.json"
out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
print(f"wrote {out}")
