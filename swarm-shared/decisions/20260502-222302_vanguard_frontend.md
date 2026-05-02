# vanguard / frontend

## Final Synthesized Plan (Correctness + Actionability)

**Guiding principle:** One lightweight, frontend-first “Discovery Pane” that unifies hub insight, HF CDN-bypass file listing, and Lightning Studio reuse—without blocking on backend work. Backend calls are proxied through a single endpoint; all other paths use local JSON or CDN URLs to avoid rate limits and quota waste.

---

## 1. Diagnosis (resolved)
- **Top-hub discovery missing** → Provide a persisted, exportable top-hub insight (MOC or highest-degree) in the pane.
- **HF rate limits on training loads** → Use a Mac CLI to list once, emit CDN-only JSON; UI consumes local file to bypass `/api/` limits.
- **No reproducible file-list artifact** → Commit `hfFileList.json` to repo; treat as build-time input for Lightning training scripts.
- **Lightning Studio reuse invisible** → Add a “Reuse Studio” section that filters running instances (backend proxy) and exposes name/id for quick reconnect.
- **Missing run-research affordance** → Add a “Run research” button that triggers backend proxy → runs `granite-business-research.sh` + `knowledge-rag` and streams status.
- **No lightweight status surface** → Mini status strip in pane for HF ingestion state, Studio jobs, and last research run.

---

## 2. Component & File Map (single canonical set)
- `/opt/axentx/vanguard/src/components/DiscoveryPane.tsx` (new)
- `/opt/axentx/vanguard/src/lib/hfFileList.json` (sample + committed)
- `/opt/axentx/vanguard/src/App.tsx` (mount pane in sidebar or dashboard route)
- `/opt/axentx/vanguard/scripts/list-hf-files.sh` (Mac orchestration)
- `/opt/axentx/vanguard/src/lib/api.ts` (lightweight proxy helpers)

---

## 3. Implementation

### 3.1 CLI helper (Mac orchestration) — CDN-bypass
```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/scripts/list-hf-files.sh
# Usage: bash list-hf-files.sh <repo> <date_folder> > ../src/lib/hfFileList.json
# Example: HF_TOKEN=hf_xxx bash list-hf-files.sh axentx/surrogate-1 2026-04-29 > ../src/lib/hfFileList.json

set -euo pipefail

REPO="${1:-axentx/surrogate-1}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"

python3 - "$REPO" "$DATE_FOLDER" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
folder = sys.argv[2]
api = HfApi()

# Single non-recursive call to avoid pagination.
tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)

files = []
for item in tree:
    if item.type == "file":
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
        files.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None)
        })

out = {
    "repo": repo,
    "folder": folder,
    "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    "files": files
}
json.dump(out, sys.stdout, indent=2)
PY
```
Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/list-hf-files.sh
```

### 3.2 Sample JSON (seed)
```json
{
  "repo": "axentx/surrogate-1",
  "folder": "2026-04-29",
  "generated_at": "2026-05-02T19:30:00Z",
  "files": [
    {
      "path": "2026-04-29/batches/mirror-merged/2026-04-29/sample.parquet",
      "cdn_url": "https://huggingface.co/datasets/axentx/surrogate-1/resolve/main/26-04-29/batches/mirror-merged/2026-04-29/sample.parquet",
      "size": 204800
    }
  ]
}
```

### 3.3 API helpers (lightweight proxy)
```ts
// /opt/axentx/vanguard/src/lib/api.ts
export async function runResearch(): Promise<{ ok: boolean; message?: string }> {
  const res = await fetch("/api/run-research", { method: "POST" });
  return res.json();
}

export async function listStudios(): Promise<Array<{ name: string; id: string; status: string }>> {
  const res = await fetch("/api/studios?status=Running");
  if (!res.ok) return [];
  return res.json();
}
```

### 3.4 Frontend: Discovery Pane (final merged)
```tsx
// /opt/axentx/vanguard/src/components/DiscoveryPane.tsx
import React, { useEffect, useState } from "react";
import { runResearch, listStudios } from "../lib/api";

type FileEntry = {
  path: string;
  cdn_url: string;
  size: number | null;
};

type HFList = {
  repo: string;
  folder: string;
  generated_at: string;
  files: FileEntry[];
};

type Studio = {
  name: string;
  id: string;
  status: string;
};

type ResearchStatus = "idle" | "running" | "done" | "error";

const DiscoveryPane: React.FC = () => {
  const [hfList, setHfList] = useState<HFList | null>(null);
  const [studios, setStudios] = useState<Studio[]>([]);
  const [topHub, setTopHub] = useState<string>("MOC");
  const [researchStatus, setResearchStatus] = useState<ResearchStatus>("idle");
  const [statusMessage, setStatusMessage] = useState<string>("");

  // Load local file list (CDN-bypass)
  useEffect(() => {
    fetch("/lib/hfFileList.json")
      .then((r) => r.json())
      .then(setHfList)
      .catch(() => setHfList(null));
  }, []);

  // Load running studios
  const refreshStudios = () => listStudios().then(setStudios).catch(() => setStudios([]));
  useEffect(() => {
    refreshStudios();
    const iv = setInterval(refreshStudios, 30000);
    return () => clearInterval(iv);
  }, []);

  const handleRunResearch = async () => {
    setResearchStatus("running");
    setStatusMessage("Running research + RAG...");
    try {
      const res = await runResearch();
      if (res.ok) {
        setResearchStatus("done");
        setStatusMessage("Research complete.");
      } else {
        setResearchStatus("error");
        setStatusMessage(res.message || "Research failed.");
      }
    } catch (err) {
      setResearchStatus("error");
      setStatusMessage(String(err));
    }
  };

  const exportTopHub = () => {
    const blob = new Blob([JSON.stringify({ topHub, exportedAt: new Date().toISOString() }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "top-hub-insight.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  const exportFileList = () => {
    if (!hfList) return;
    const blob = new Blob([JSON.stringify(hfList, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${hfList.repo}-${hfList.folder}-files.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

 
