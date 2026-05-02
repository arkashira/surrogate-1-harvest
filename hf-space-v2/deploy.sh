#!/usr/bin/env bash
# deploy.sh — push hf-space-v2/* to HF Hub as a brand-new Space.
#
# Idempotent: re-running just pushes the latest files. Creates the Space
# if missing. Auto-promotes to ZeroGPU A10G if the HF_TOKEN is on a PRO
# account (PRO is required for free ZeroGPU minutes).
#
# Required env (export before running, sourced from ~/.note):
#   HF_TOKEN           HF token with `write` scope on the target namespace
#                      (default: HF_TOKEN_PRO_WRITE — ashirato user, has
#                      ZeroGPU eligibility under ashirato/* namespace)
#   SPACE_ID           e.g. ashirato/surrogate-1-v2  OR  axentx/surrogate-1-v2
#                      Use ashirato/* for free ZeroGPU; axentx/* needs
#                      org Team plan ($60/mo).
#   ADAPTER_REPO       (optional) defaults to axentx/surrogate-1-coder-7b-lora-v2
#                      — must exist before deploying or the Space will boot
#                      without an adapter and just serve the base model.
set -euo pipefail

HF_TOKEN="${HF_TOKEN:-}"
SPACE_ID="${SPACE_ID:-ashirato/surrogate-1-v2}"
ADAPTER_REPO="${ADAPTER_REPO:-axentx/surrogate-1-coder-7b-lora-v2}"
HARDWARE="${HARDWARE:-zero-a10g}"

if [ -z "$HF_TOKEN" ]; then
    echo "FATAL: HF_TOKEN env not set. Export before running." >&2
    exit 1
fi

cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"

echo "▸ Surrogate-1 v2 → $SPACE_ID  (hw=$HARDWARE)"

# 1. Verify the LoRA adapter exists on Hub. If not, warn but proceed —
#    the Space can serve the base model alone.
if ! curl -fsS -m 8 -H "Authorization: Bearer $HF_TOKEN" \
       "https://huggingface.co/api/models/$ADAPTER_REPO" >/dev/null; then
    echo "⚠  adapter $ADAPTER_REPO not found on Hub."
    echo "   The Space will boot serving the base model only."
    echo "   Run notebooks/v2-train-colab.ipynb on Colab (T4 free) to train + push."
    sleep 2
fi

# 2. Create the Space if missing (huggingface_hub is the cleanest API).
python3 - <<PY
import os
from huggingface_hub import HfApi, RepoUrl
api = HfApi(token="$HF_TOKEN")
sid = "$SPACE_ID"
try:
    info = api.space_info(sid)
    print(f"  ✓ Space {sid} exists")
except Exception:
    print(f"  ▸ creating Space {sid}")
    api.create_repo(
        repo_id=sid,
        repo_type="space",
        space_sdk="gradio",
        private=False,
    )
PY

# 3. Push files. We use snapshot upload via huggingface_hub for atomic
#    behavior — partial uploads don't leave a half-broken Space.
python3 - <<PY
import os
from huggingface_hub import HfApi
api = HfApi(token="$HF_TOKEN")
api.upload_folder(
    folder_path="$SCRIPT_DIR",
    repo_id="$SPACE_ID",
    repo_type="space",
    commit_message="deploy: surrogate-1-v2 app + reqs",
    ignore_patterns=["deploy.sh", ".git/**", "__pycache__/**", "*.pyc"],
)
print(f"  ✓ uploaded {os.path.basename('$SCRIPT_DIR')}/* to {('$SPACE_ID')}")
PY

# 4. Promote to ZeroGPU A10G if HF_TOKEN owner is PRO. Best-effort.
python3 - <<PY
import os
from huggingface_hub import HfApi
api = HfApi(token="$HF_TOKEN")
try:
    api.request_space_hardware(repo_id="$SPACE_ID", hardware="$HARDWARE")
    print(f"  ✓ requested $HARDWARE")
except Exception as e:
    print(f"  ⚠ hardware request: {e}")
    print(f"    (Free CPU tier still works for smoke tests — promote manually")
    print(f"    via the Space UI if you have PRO/Team budget.)")
PY

# 5. Set Secrets so the Space can pull private adapters / send telemetry.
python3 - <<PY
import os
from huggingface_hub import HfApi
api = HfApi(token="$HF_TOKEN")
secrets = {
    "ADAPTER_REPO": "$ADAPTER_REPO",
    "BASE_MODEL": "Qwen/Qwen2.5-Coder-7B-Instruct",
    "HF_TOKEN": "$HF_TOKEN",  # adapter pull on private repos
}
for k, v in secrets.items():
    try:
        api.add_space_secret(repo_id="$SPACE_ID", key=k, value=v)
        print(f"  ✓ secret {k} set")
    except Exception as e:
        # add fails if secret exists — fall back to overwrite
        try:
            api.delete_space_secret(repo_id="$SPACE_ID", key=k)
            api.add_space_secret(repo_id="$SPACE_ID", key=k, value=v)
            print(f"  ✓ secret {k} updated")
        except Exception as e2:
            print(f"  ⚠ secret {k}: {e2}")
PY

# 6. Restart so the new code + adapter env take effect.
python3 - <<PY
from huggingface_hub import HfApi
api = HfApi(token="$HF_TOKEN")
try:
    api.restart_space(repo_id="$SPACE_ID")
    print(f"  ✓ restart triggered")
except Exception as e:
    print(f"  ⚠ restart: {e}")
PY

echo ""
echo "✓ deploy complete — https://huggingface.co/spaces/$SPACE_ID"
echo "  First boot: ~2-3 min while Gradio + transformers warm up."
echo "  After that: per-request ~3-8 s on A10G ZeroGPU."
