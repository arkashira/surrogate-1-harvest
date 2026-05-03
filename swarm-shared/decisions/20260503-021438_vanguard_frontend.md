# vanguard / frontend

## 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` manifest: every training run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- Frontend has no UI to generate or view the file-list manifest, so operators manually re-run listing and copy-paste paths (error-prone).
- Training/data loader likely uses `load_dataset(...)` or per-file streaming on heterogeneous repos, causing `pyarrow.CastError` on mixed schemas.
- No CDN-bypass strategy exposed in frontend: training scripts still rely on authenticated API calls during data loading instead of using public CDN URLs.
- Missing lightweight frontend utility to pre-list a single date folder and emit a `file-list.json` that training scripts can consume (zero API calls during training).

## 2. Proposed change

Add a small frontend utility page/component in `/opt/axentx/vanguard` that:
- Accepts `repo` (e.g., `datasets/username/repo`) and `dateFolder` (e.g., `2026-05-03`)
- Calls HF API **once** (from the browser/backend proxy) using `list_repo_tree(path=dateFolder, recursive=False)`
- Persists `file-list.json` to `manifests/{repo_slug}/{dateFolder}.json`
- Displays the list and a CDN-bypass training snippet (copy-paste ready)

Scope:
- Add `src/manifest-generator.{js,tsx}` (or equivalent frontend entry)
- Add `manifests/` directory for outputs
- Add minimal UI route/button in existing frontend layout

## 3. Implementation

Below is a minimal, copy-paste-ready implementation for a React-like frontend (adjust to your actual stack). If you’re on plain JS, drop the JSX and use DOM APIs.

Create: `src/ManifestGenerator.jsx`

```jsx
import { useState } from "react";

const HF_API_BASE = "https://huggingface.co/api";

function repoToPath(repo) {
  // repo input: datasets/username/repo  or username/repo
  return repo.replace(/^datasets\//, "");
}

async function listRepoTree(repo, path = "") {
  const r = repoToPath(repo);
  const url = `${HF_API_BASE}/repos/${r}/tree/${encodeURIComponent(path)}?recursive=false`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${process.env.REACT_APP_HF_TOKEN}` }
  });
  if (!res.ok) throw new Error(`HF API error: ${res.status}`);
  return res.json(); // array of { path, type }
}

function buildFileList(entries, dateFolder) {
  // Keep only files (not dirs) and produce CDN URLs
  return entries
    .filter((e) => e.type === "file")
    .map((e) => ({
      path: e.path,
      cdn_url: `https://huggingface.co/datasets/${repoToPath(
        dateFolder.includes("/") ? dateFolder.split("/")[0] : "repo-placeholder"
      )}/resolve/main/${encodeURIComponent(e.path)}`
    }));
}

export default function ManifestGenerator() {
  const [repo, setRepo] = useState("");
  const [dateFolder, setDateFolder] = useState("");
  const [loading, setLoading] = useState(false);
  const [manifest, setManifest] = useState(null);
  const [error, setError] = useState("");

  const generate = async () => {
    if (!repo || !dateFolder) return;
    setLoading(true);
    setError("");
    try {
      const entries = await listRepoTree(repo, dateFolder);
      const files = buildFileList(entries, repo);
      const payload = { repo, dateFolder, generatedAt: new Date().toISOString(), files };
      setManifest(payload);

      // Trigger download of manifest
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `manifests/${repo.replace(/\//g, "_")}_${dateFolder}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 720, padding: 20 }}>
      <h2>Generate file-list manifest (CDN-bypass)</h2>
      <p>
        Run once per (repo, dateFolder) to create a manifest. Training will use CDN URLs and make zero HF API calls.
      </p>

      <label>
        Repo (e.g. datasets/username/repo):
        <input value={repo} onChange={(e) => setRepo(e.target.value)} style={{ width: "100%", marginTop: 4 }} />
      </label>

      <label>
        Date folder (e.g. 2026-05-03):
        <input value={dateFolder} onChange={(e) => setDateFolder(e.target.value)} style={{ width: "100%", marginTop: 4 }} />
      </label>

      <button onClick={generate} disabled={loading || !repo || !dateFolder} style={{ marginTop: 12 }}>
        {loading ? "Generating..." : "Generate manifest"}
      </button>

      {error && <pre style={{ color: "red" }}>{error}</pre>}

      {manifest && (
        <>
          <h3>Generated manifest</h3>
          <pre style={{ background: "#f6f8fa", padding: 12, overflow: "auto" }}>
            {JSON.stringify(manifest, null, 2)}
          </pre>

          <h3>Training snippet (CDN-only)</h3>
          <pre style={{ background: "#f6f8fa", padding: 12, overflow: "auto" }}>
{`# Save the downloaded manifest to manifests/ and use in training:
import json
import requests

with open("manifests/${repo.replace(/\//g, "_")}_${dateFolder}.json") as f:
    manifest = json.load(f)

def cdn_data_loader(manifest):
    for item in manifest["files"]:
        # stream from CDN (no auth, no API quota)
        resp = requests.get(item["cdn_url"], stream=True)
        resp.raise_for_status()
        yield resp.content  # project to {prompt,response} here
`}
          </pre>
        </>
      )}
    </div>
  );
}
```

Add route/integration (example for React Router):

```jsx
// In your routes file
import ManifestGenerator from "./ManifestGenerator";
<Route path="/manifest-generator" element={<ManifestGenerator />} />
```

Create output directory:

```bash
mkdir -p /opt/axentx/vanguard/manifests
```

Environment: add `REACT_APP_HF_TOKEN` to frontend `.env` (or proxy via backend if you prefer not to expose token to browser).

## 4. Verification

1. Open `/manifest-generator` in the frontend.
2. Enter a repo (e.g., `datasets/example/repo`) and a date folder (e.g., `2026-05-03`).
3. Click “Generate manifest” and confirm:
   - A `manifests/...json` file downloads.
   - The file contains `files[]` with `path` and `cdn_url`.
   - `cdn_url` follows `https://huggingface.co/datasets/.../resolve/main/...`.
4. Confirm no authenticated HF API calls occur during generation (network tab: only one `tree` call; no repeated calls).
5. Copy the training snippet into a Lightning training script and verify it can iterate files via CDN without setting `Authorization` headers (should succeed even after HF API rate limits).
