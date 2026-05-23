#!/usr/bin/env bash
# Surrogate-1 v2 — Spawn an additional sharded HF Space.
#
# Each new Space duplicates axentx/surrogate-1 Docker stack but seeds
# coordinator with only its hash-slice of sources via SHARD_INDEX env.
# SHARD_TOTAL=3 (Space 1=axentx default, Space 2=ashirato, Space 3=surrogate1).
#
# Net: 3 parallel cpu-basic 16GB Spaces = 3x worker capacity + 3x dedup'd
# sources. Each pushes to axentx/surrogate-1-pairs-{A,B,C,D} (round-robin).
#
# Usage:
#   spawn-shard-space.sh <namespace> <shard_index> <write_token>
# Example (substitute actual tokens from ~/.hermes/.env):
#   spawn-shard-space.sh ashirato 1 "$HF_TOKEN_PRO_WRITE"
#   spawn-shard-space.sh surrogate1 2 "$HF_TOKEN"
set -uo pipefail

NS="${1:-}"
SHARD="${2:-}"
TOKEN="${3:-}"
SHARD_TOTAL="${SHARD_TOTAL:-3}"
SOURCE_REPO="${SOURCE_REPO:-axentx/surrogate-1}"
TARGET_NAME="${TARGET_NAME:-surrogate-1-shard${SHARD}}"

[[ -z "$NS" || -z "$SHARD" || -z "$TOKEN" ]] && {
    echo "usage: $0 <namespace> <shard_index> <write_token>" >&2
    echo "example: $0 ashirato 1 hf_WEzFKXh..." >&2
    exit 2
}

REPO="${NS}/${TARGET_NAME}"

echo "▶ create Space ${REPO} (cpu-basic, docker SDK)"
HF_WRITE_TOKEN="$TOKEN" python3 - <<PYEOF
import os, sys
from huggingface_hub import HfApi, create_repo

token = os.environ["HF_WRITE_TOKEN"]
api = HfApi(token=token)
repo = "$REPO"
src = "$SOURCE_REPO"

try:
    create_repo(repo_id=repo, repo_type="space", space_sdk="docker",
                token=token, exist_ok=True, private=False)
    print(f"  ✓ Space repo: {repo}")
except Exception as e:
    print(f"  Space create: {e}", file=sys.stderr)

# Set secrets — copy from primary token pool (env vars, never hardcoded —
# HF Space pre-receive hook rejects pushes containing token strings).
import os as _os
SECRETS = {
    "HF_TOKEN":           _os.environ.get("HF_TOKEN", ""),
    "HF_TOKEN_PRO_WRITE": _os.environ.get("HF_TOKEN_PRO_WRITE", ""),
    "HF_TOKEN_2":         _os.environ.get("HF_TOKEN_2", ""),
    "HF_TOKEN_3":         _os.environ.get("HF_TOKEN_3", ""),
    "HF_TOKEN_4":         _os.environ.get("HF_TOKEN_4", ""),
    "HF_TOKEN_PRO":       _os.environ.get("HF_TOKEN_PRO", _os.environ.get("HF_TOKEN", "")),
    "HF_TOKEN_POOL":      _os.environ.get("HF_TOKEN_POOL", ""),
}
SECRETS = {k: v for k, v in SECRETS.items() if v}  # drop empties
for k, v in SECRETS.items():
    try:
        api.add_space_secret(repo_id=repo, key=k, value=v,
            description=f"shard ${SHARD}/$SHARD_TOTAL — copied from primary")
        print(f"  ✓ secret {k}")
    except Exception as e:
        print(f"  ! secret {k}: {str(e)[:120]}")

# Add SHARD env vars (visible at runtime, not secret)
ENVS = {
    "SHARD_INDEX": "$SHARD",
    "SHARD_TOTAL": "$SHARD_TOTAL",
    "LOW_MEM": "1",
    "DISCORD_WEBHOOK": os.environ.get("DISCORD_WEBHOOK", ""),
}
for k, v in ENVS.items():
    if not v: continue
    try:
        api.add_space_variable(repo_id=repo, key=k, value=v,
            description=f"shard config")
        print(f"  ✓ env {k}={v}")
    except Exception as e:
        print(f"  ! env {k}: {str(e)[:120]}")

# Mirror entire Space repo from source (clone + push to new namespace)
print(f"  ▶ mirroring files from {src}")
import tempfile, subprocess
with tempfile.TemporaryDirectory() as td:
    subprocess.run(["git", "clone",
                    f"https://USER:{token}@huggingface.co/spaces/{src}",
                    f"{td}/src"], check=True, capture_output=True)
    subprocess.run(["git", "-C", f"{td}/src", "remote", "add", "shard",
                    f"https://USER:{token}@huggingface.co/spaces/{repo}"],
                   capture_output=True)
    r = subprocess.run(["git", "-C", f"{td}/src", "push", "shard", "main", "--force"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  ✓ files pushed to {repo}")
    else:
        print(f"  ! push: {r.stderr[:300]}")

print(f"\n→ https://huggingface.co/spaces/{repo}")
PYEOF
