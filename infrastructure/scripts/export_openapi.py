"""Write the OpenAPI schema to docs/api/openapi.json (generated; do not edit)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

app = create_app(Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", log_json=False))
out = ROOT / "docs" / "api" / "openapi.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
print(f"wrote {out} ({len(app.openapi()['paths'])} paths)")
