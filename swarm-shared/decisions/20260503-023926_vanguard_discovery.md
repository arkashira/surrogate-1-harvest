# vanguard / discovery

## 1. Diagnosis

- Frontend still triggers authenticated HF API calls (`list_repo_tree`, dataset endpoints) at runtime, burning quota and risking 429s on user machines.
- No static file manifest embedded in the bundle → every session re-enumerates the repo instead of using a pre-listed, CDN-only file list.
- Missing `index.html` and client mount point → no fast feedback loop for frontend discovery changes.
- No asset pipeline or bundler configured → cannot embed build-time artifacts (file manifest) into the frontend.
- No clear separation between orchestration (Mac/CLI) and runtime (browser) responsibilities → violates Mac=CLI rule and leaks HF API usage to the client.

## 2. Proposed change

- Create `/opt/axentx/vanguard/public/index.html` as the minimal client entry.
- Add `/opt/axentx/vanguard/scripts/build-manifest.py` (run on Mac) that:
  - Uses HF API **once** (after rate-limit window) to `list_repo_tree(path, recursive=False)` for a target date folder.
  - Emits `public/manifest.json` containing `{ "date": "...", "files": ["path1", "path2", ...] }`.
- Add `/opt/axentx/vanguard/public/app.js` that:
  - Loads `manifest.json` at startup.
  - Fetches dataset files **only via CDN** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header.
- Add `/opt/axentx/vanguard/.gitignore` entries for `public/manifest.json` (build artifact) and local caches.

## 3. Implementation

```bash
# 1) Create public mount and index.html
mkdir -p /opt/axentx/vanguard/public
cat > /opt/axentx/vanguard/public/index.html <<'EOF'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Discovery</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; }
    #status { color: #666; font-size: 0.9rem; margin-bottom: 1rem; }
    ul { list-style: none; padding: 0; }
    li { padding: 0.25rem 0; }
    .error { color: #b00020; }
  </style>
</head>
<body>
  <h1>Vanguard — Discovery</h1>
  <div id="status">Loading manifest…</div>
  <ul id="files"></ul>

  <script src="/app.js"></script>
</body>
</html>
EOF

# 2) Create app.js (CDN-only fetches, no HF API from browser)
cat > /opt/axentx/vanguard/public/app.js <<'EOF'
const REPO = "your-org/your-dataset-repo"; // adjust via build script or env
const manifestPath = "/manifest.json";

function el(id) { return document.getElementById(id); }

async function loadManifest() {
  try {
    const res = await fetch(manifestPath, { cache: "no-store" });
    if (!res.ok) throw new Error(`Manifest fetch failed: ${res.status}`);
    return await res.json();
  } catch (err) {
    el("status").textContent = "Failed to load manifest.";
    el("status").classList.add("error");
    console.error(err);
    throw err;
  }
}

function renderFiles(files) {
  const list = el("files");
  list.innerHTML = "";
  if (!files || files.length === 0) {
    list.innerHTML = "<li>No files listed in manifest.</li>";
    return;
  }
  files.forEach((path) => {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = `https://huggingface.co/datasets/${REPO}/resolve/main/${encodeURIComponent(path)}`;
    link.textContent = path;
    link.target = "_blank";
    link.rel = "noopener";
    li.appendChild(link);
    list.appendChild(li);
  });
}

(async () => {
  el("status").textContent = "Loading manifest…";
  try {
    const manifest = await loadManifest();
    el("status").textContent = `Date: ${manifest.date || "unknown"} — ${(manifest.files || []).length} files (CDN)`;
    renderFiles(manifest.files || []);
  } catch {
    el("status").textContent = "Could not load file list.";
    el("status").classList.add("error");
  }
})();
EOF

# 3) Create build-manifest.py (run on Mac — orchestration only)
mkdir -p /opt/axentx/vanguard/scripts
cat > /opt/axentx/vanguard/scripts/build-manifest.py <<'PY'
#!/usr/bin/env python3
"""
Usage (Mac orchestration):
  HF_TOKEN=hf_xxx python3 scripts/build-manifest.py \
    --repo your-org/your-dataset-repo \
    --date-folder 2026-05-03 \
    --out public/manifest.json

Notes:
- Single API call to list_repo_tree (non-recursive) for one date folder.
- Output is intended to be committed or copied into public/ for CDN-only browser usage.
- After 429, wait 360s before retrying.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from huggingface_hub import HfApi, RepositoryNotFoundError
except ImportError:
    print("Error: huggingface_hub not installed. Install via: pip install huggingface_hub")
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Build CDN file manifest for a dataset date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/name)")
    parser.add_argument("--date-folder", required=True, help="Date folder path inside repo (e.g., 2026-05-03)")
    parser.add_argument("--out", default="public/manifest.json", help="Output JSON path")
    parser.add_argument("--retry-wait", type=int, default=360, help="Seconds to wait after 429")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN") or os.environ.get("HF_API_TOKEN")
    api = HfApi(token=token)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    attempt = 0
    max_attempts = 3
    backoff = args.retry_wait

    while attempt < max_attempts:
        try:
            # Non-recursive list for the specific date folder
            items = api.list_repo_tree(
                repo_id=args.repo,
                path=args.date_folder,
                repo_type="dataset",
                recursive=False,
            )
            # items can be dicts or objects depending on hf_hub version — normalize
            filenames = []
            for it in items:
                if isinstance(it, dict):
                    f = it.get("path") or it.get("name")
                else:
                    f = getattr(it, "path", None) or getattr(it, "name", None)
                if f:
                    filenames.append(f)

            # Sort for deterministic output
            filenames = sorted(filenames)

            manifest = {
                "repo": args.repo,
                "date": args.date_folder,
                "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "files": filenames,
            }

            out_path.write_text(json.dumps(manifest, indent=2) + "\n")
            print(f"Manifest written to {out_path} ({len(filenames)} files)")
            return 0

        except RepositoryNotFoundError:
            print(f"Error: repo {args.repo} not found.")
            return 1
        except Exception as exc:
            # Detect rate limit (429) by message or status if available
            msg = str(exc).lower()
            if "429" in msg or "rate limit" in msg or "too many requests" in msg:
                attempt += 1
                if attempt >= max_attempts:
                    print(f"Rate-limited and max attempts reached
