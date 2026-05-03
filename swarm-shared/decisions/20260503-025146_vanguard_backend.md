# vanguard / backend

Below is the single, consolidated plan that keeps the strongest, most compatible pieces from both proposals and makes them correct, minimal, and immediately actionable.

- **Project layout**: `vanguard/` package (not `src/`) to avoid nested `src` indirection in `/opt/axentx/vanguard`.
- **Dependency management**: `pyproject.toml` only (Poetry-style) — one source of truth; omit redundant `requirements.txt`.
- **Config**: Pydantic `BaseSettings` via `pydantic-settings` (modern, typed, `.env`-driven).
- **Service**: FastAPI app factory with `/health` (and placeholders for `/train/*`, `/hf/file-list`) and graceful uvicorn launcher.
- **HF strategy**: include a small `generate_file_list.py` script to produce `file_list.json` for CDN-only ingestion and sibling rotation.
- **Launcher**: `scripts/run-dev.sh` for local dev (with reload) and `vanguard/server.py` for cron/systemd-safe production start.
- **No changes to existing code** (none present); only new files.

Implementation (run once):

```bash
cd /opt/axentx/vanguard

# 1) Project metadata + deps (single source of truth)
cat > pyproject.toml <<'EOF'
[tool.poetry]
name = "vanguard"
version = "0.1.0"
description = "Vanguard backend service"
authors = ["Axentx Team"]

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110"
uvicorn = { extras = ["standard"], version = "^0.29" }
python-dotenv = "^1.0"
pydantic-settings = "^2.7"
httpx = "^0.27"
huggingface-hub = "^0.24"
lightning = "^2.3"

[tool.poetry.scripts]
vanguard = "vanguard.server:main"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"
EOF

# 2) Example env and gitignore
cat > .env.example <<'EOF'
# Server
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=info

# HF ingestion
HF_REPO=org/surrogate-1
HF_FILE_LIST_PATH=file_list.json
HF_RATE_LIMIT_WAIT=360

# Lightning
LIGHTNING_TEAMSPACE=default
LIGHTNING_CLOUDS_PRIORITY=lightning-lambda-prod,lightning-aws-us-east-1

# Secrets (examples)
# HF_TOKEN=
# KAGGLE_TOKEN=
EOF

cat > .gitignore <<'EOF'
__pycache__/
*.py[cod]
.env
.venv/
venv/
dist/
.coverage
.pytest_cache/
EOF

# 3) Package scaffold
mkdir -p vanguard scripts
touch vanguard/__init__.py

# Config (pydantic-settings)
cat > vanguard/config.py <<'EOF'
from pydantic_settings import BaseSettings
from typing import List, Optional

class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # HF ingestion
    hf_repo: str = "org/surrogate-1"
    hf_sibling_repos: List[str] = [
        "org/surrogate-1",
        "org/surrogate-1-sib1",
        "org/surrogate-1-sib2",
        "org/surrogate-1-sib3",
        "org/surrogate-1-sib4",
    ]
    hf_cdn_base: str = "https://huggingface.co/datasets"
    hf_file_list_path: str = "file_list.json"
    hf_rate_limit_wait: int = 360

    # Lightning
    lightning_teamspace: str = "default"
    lightning_clouds_priority: List[str] = [
        "lightning-lambda-prod",
        "lightning-aws-us-east-1",
    ]

    # Secrets (optional)
    hf_token: Optional[str] = None
    kaggle_token: Optional[str] = None

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
EOF

# Main app factory + endpoints
cat > vanguard/main.py <<'EOF'
import logging
from fastapi import FastAPI
from vanguard.config import settings

def create_app() -> FastAPI:
    app = FastAPI(title="Vanguard API", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "vanguard"}

    @app.post("/train/start")
    async def train_start() -> dict:
        # Placeholder: integrate Lightning Studio launcher here
        return {"status": "started", "note": "train job placeholder"}

    @app.get("/train/status")
    async def train_status() -> dict:
        # Placeholder: query running job state
        return {"status": "unknown", "note": "status query placeholder"}

    @app.get("/hf/file-list")
    async def hf_file_list() -> dict:
        # Placeholder: serve or regenerate CDN file list
        return {"status": "ok", "path": str(settings.hf_file_list_path)}

    @app.on_event("startup")
    async def startup() -> None:
        logging.info(
            "Vanguard started | host=%s port=%s",
            settings.host,
            settings.port,
        )

    return app

app = create_app()
EOF

# Server launcher (cron/systemd-safe)
cat > vanguard/server.py <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(realpath "$0")")/.."

if [ -d ".venv" ]; then
  source .venv/bin/activate
elif [ -d "venv" ]; then
  source venv/bin/activate
fi

export SHELL=/bin/bash
exec python -m uvicorn vanguard.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8000}" \
  --log-level "${LOG_LEVEL:-info}"
EOF

chmod +x vanguard/server.py

# Dev helper (reload enabled)
cat > scripts/run-dev.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."

if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi

export SHELL=/bin/bash
exec python -m uvicorn vanguard.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8000}" \
  --reload \
  --log-level "${LOG_LEVEL:-info}"
EOF

chmod +x scripts/run-dev.sh

# HF file-list generator (CDN-first ingestion)
cat > scripts/generate_file_list.py <<'EOF'
#!/usr/bin/env python3
"""
Generate file_list.json for CDN-only ingestion.
Call HF once, save the list, then train with zero API calls.
"""
import json
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi
from vanguard.config import settings

def main() -> None:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    repo = settings.hf_repo
    out_path = Path(settings.hf_file_list_path)

    files = api.list_repo_files(repo=repo)
    mapping = {
        "repo": repo,
        "siblings": settings.hf_sibling_repos,
        "files": sorted(files),
        "cdn_base": settings.hf_cdn_base,
    }
    out_path.write_text(json.dumps(mapping, indent=2))
    print(f"Wrote {len(files)} entries to {out_path}")

if __name__ == "__main__":
    main()
EOF

chmod +x scripts/generate_file_list.py
```

Verification (same as Candidate 1, plus HF script):

```bash
cd /opt/axentx/vanguard

# Install and run
python -m pip install poetry
poetry install
poetry run vanguard &
sleep 3

# Health check

