# vanguard / backend

## Final consolidated implementation

**File:** `/opt/axentx/vanguard/src/backend/training/training_launcher.py`

```python
"""
Durable training launcher for HF datasets on Lightning.

Key guarantees:
- HF tree is listed once per date_folder and persisted as a manifest.
- Training always uses the manifest (CDN-only fetches; no repeated API calls).
- Sweeps Lightning machines in priority order (H200 → L40S → V100) and falls back
  cleanly instead of blocking on unavailable machines.
- Reuses Running studios by name; restarts idle-stopped studios before .run().
- Avoids quota waste and 429s; safe for repeated launcher invocations.
"""

import json
import os
import time
from pathlib import Path
from typing import List, Tuple, Optional

import requests
from lightning.app import LightningWork, LightningFlow, LightningApp, Studio
from lightning.app.utilities.exceptions import LightningException

HF_REPO = os.getenv("HF_REPO", "datasets/your-org/your-dataset")
HF_TOKEN = os.getenv("HF_TOKEN", "")
MANIFEST_DIR = Path(__file__).parent.parent.parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)


# -------------------------
# Manifest handling
# -------------------------
def _manifest_path(date_folder: str) -> Path:
    safe = date_folder.strip("/").replace("/", "_")
    return MANIFEST_DIR / f"{safe}_manifest.json"


def list_hf_folder(date_folder: str, *, timeout: int = 30) -> List[str]:
    """
    Non-recursive HF tree listing for a top-level date folder.
    Retries once on 429 with backoff. Falls back to requiring a prebuilt
    manifest on persistent failure to avoid repeated quota burn.
    """
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    url = f"https://huggingface.co/api/datasets/{HF_REPO}/tree"
    params = {"path": date_folder, "recursive": "false"}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code == 429:
            time.sleep(5)
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        items = resp.json()
        return [item["path"] for item in items if item.get("type") == "file"]
    except Exception as exc:
        raise RuntimeError(
            f"HF tree listing failed for '{date_folder}'. "
            f"Provide manifest manually at {_manifest_path(date_folder)}."
        ) from exc


def save_manifest(date_folder: str, files: List[str]) -> Path:
    manifest = {"date_folder": date_folder, "files": files}
    path = _manifest_path(date_folder)
    path.write_text(json.dumps(manifest, indent=2))
    return path


def load_or_build_manifest(date_folder: str, *, rebuild: bool = False) -> Path:
    path = _manifest_path(date_folder)
    if not rebuild and path.exists():
        return path
    files = list_hf_folder(date_folder)
    return save_manifest(date_folder, files)


# -------------------------
# Machine priority sweep
# -------------------------
def pick_machine_priority() -> List[Tuple[str, str]]:
    """
    Priority order for Lightning machines.
    Returns list of (cloud_account, machine_name).
    """
    return [
        ("lightning-lambda-prod", "H200"),
        ("lightning-public-prod", "L40S"),
        ("lightning-public-prod", "V100"),
    ]


# -------------------------
# Launcher
# -------------------------
class TrainingLauncher(LightningWork):
    """
    Orchestrates:
    - manifest creation/reuse
    - studio reuse / restart / create
    - machine priority sweep
    - training script execution with manifest injected via env
    """

    def __init__(
        self,
        studio_name: str,
        date_folder: str,
        script_path: str,
        rebuild_manifest: bool = False,
    ):
        super().__init__()
        self.studio_name = studio_name
        self.date_folder = date_folder
        self.script_path = script_path
        self.rebuild_manifest = rebuild_manifest

    def _find_running_studio(self):
        # Avoids heavy imports at top-level; safe within run()
        from lightning.app import Teamspace

        teamspace = Teamspace()
        for studio in teamspace.studios:
            if studio.name == self.studio_name and getattr(studio, "status", None) == "running":
                return studio
        return None

    def _launch_studio(self) -> Studio:
        clouds_machines = pick_machine_priority()
        last_error = None

        for cloud, machine in clouds_machines:
            try:
                studio = Studio(
                    name=self.studio_name,
                    script_path=self.script_path,
                    machine=machine,
                    cloud=cloud,
                    create_ok=True,
                )
                print(f"Launched studio '{self.studio_name}' on {cloud}/{machine}")
                return studio
            except LightningException as exc:
                last_error = exc
                print(f"Failed to launch on {cloud}/{machine}: {exc}")
                continue

        raise RuntimeError(
            f"Could not launch studio '{self.studio_name}' on any available machine."
        ) from last_error

    def _ensure_studio_running(self, studio: Studio) -> Studio:
        if getattr(studio, "status", None) == "running":
            return studio

        print(f"Studio '{self.studio_name}' is {studio.status}. Restarting...")
        cloud, machine = pick_machine_priority()[0]
        studio.start(machine=machine, cloud=cloud)

        timeout = 120
        start_at = time.time()
        while time.time() - start_at < timeout:
            if getattr(studio, "status", None) == "running":
                return studio
            time.sleep(5)

        raise RuntimeError(f"Studio '{self.studio_name}' failed to start within {timeout}s.")

    def run(self):
        # 1) manifest (CDN-only training will use this)
        manifest = load_or_build_manifest(self.date_folder, rebuild=self.rebuild_manifest)
        print(f"Using manifest: {manifest}")

        # 2) studio reuse/create
        studio = self._find_running_studio()
        if studio is None:
            studio = self._launch_studio()

        # 3) ensure running
        studio = self._ensure_studio_running(studio)

        # 4) execute training with manifest
        env = {
            "HF_MANIFEST_PATH": str(manifest),
            "HF_REPO": HF_REPO,
            "DATE_FOLDER": self.date_folder,
        }
        result = studio.run(script_path=self.script_path, env=env)
        print(f"Training run completed: {result}")
        return result


# -------------------------
# Minimal flow driver
# -------------------------
class TrainingFlow(LightningFlow):
    def __init__(self):
        super().__init__()
        self.launcher = TrainingLauncher(
            studio_name="vanguard-training",
            date_folder="batches/mirror-merged/2026-04-29",
            script_path="scripts/train_surrogate.py",
            rebuild_manifest=False,
        )

    def run(self):
        self.launcher.run()


if __name__ == "__main__":
    # For direct testing/orchestration runs
    app = LightningApp(TrainingFlow())
```

## Verification checklist

1. Save the file at `/opt/axentx/vanguard/src/backend/training/training_launcher.py`.
2. Set environment variables:
   - `HF_REPO` (e.g., `datasets/your-org/your-dataset`)
   - `HF_TOKEN` (only required if private repo or higher rate limits needed)
3. Quick smoke test (manifest-only):
   ```bash
   cd /opt/axentx/vanguard
   python -c "
   from src.backend.training.training_launcher import load_or_build_manifest
   m = load_or_build_manifest('batches/mirror-merged/2026-04-29')
   print(m.read_text())
   "
   ```
4. Run the launcher (orchestration + studio):
   ```bash
   cd /opt/
