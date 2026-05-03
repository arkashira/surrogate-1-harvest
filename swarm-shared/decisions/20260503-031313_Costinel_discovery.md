# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

**Goal:** Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel that surfaces the most-connected hub (default `MOC`) with 3 contextual insights from knowledge-rag. Zero blocking on main dashboard load, graceful fallback, and cron-safe updates.

---

## Architecture (CDN-first, edge-friendly)

```
Costinel Dashboard (React)
  └─ TopHubSignalPanel (lazy + Suspense)
       ├─ useSWR (client) → /api/top-hub?slug=moc
       └─ /api/top-hub (edge handler)
            ├─ Reads /public/hubs/index.json (pre-generated)
            └─ Streams latest insights JSON from CDN:
               https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/insights/{date}/{slug}.json
               (no Authorization header; CDN cacheable)
```

- **Why this wins:** Combines Candidate 1’s simplicity (local CDN files + cron) with Candidate 2’s robustness (SWR, skeletons, edge handler, proper typing, and graceful CDN fallback). Removes blocking, avoids HF API during render, and keeps UI fast.

---

## Tasks (≤2h)

1. **Create file lister + cron manifest**  
   `/opt/axentx/Costinel/scripts/list-hub-files.sh` — generates `/public/hubs/index.json` and per-hub CDN-ready JSON (with fallback local content).

2. **Add sample CDN hub file**  
   `/public/hubs/moc.json` — local fallback used by cron and dev.

3. **Add API route**  
   `/pages/api/top-hub.ts` (or `/app/api/top-hub/route.ts` for Next.js App Router) — resolves hub file, fetches from CDN with timeout + fallback to local file.

4. **Add React component**  
   `TopHubSignalPanel.tsx` — lazy, SWR, skeletons, 3 insights, badges, and non-blocking.

5. **Mount in dashboard**  
   Add Suspense boundary in dashboard layout.

6. **Add cron entry**  
   Runs after `market-analysis` with `SHELL=/bin/bash`.

---

## 1) list-hub-files.sh (CDN file lister + local fallback generator)

```bash
#!/usr/bin/env bash
# /opt/axentx/Costinel/scripts/list-hub-files.sh
# Generates hub index and CDN-ready local fallback JSON files.
# Run after market-analysis (cron-safe).

set -euo pipefail

REPO_ROOT="/opt/axentx/Costinel"
OUTPUT_DIR="${REPO_ROOT}/public/hubs"
MANIFEST="${OUTPUT_DIR}/index.json"
CDN_BASE="https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/insights"

mkdir -p "${OUTPUT_DIR}"

# Hub definitions (add more as needed)
cat > "${MANIFEST}" <<EOF
{
  "generated": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "hubs": [
    { "slug": "moc", "folder": "2026-04-27", "title": "MOC", "priority": 1 },
    { "slug": "cloud-governance", "folder": "2026-04-27", "title": "Cloud Governance", "priority": 2 }
  ]
}
EOF

# Generate local fallback JSON for each hub (used if CDN fails or in dev)
generate_local_fallback() {
  local slug="$1"
  local folder="$2"
  local out="${OUTPUT_DIR}/${slug}.json"

  case "$slug" in
    moc)
      cat > "${out}" <<'JSON'
{
  "hub": "MOC",
  "slug": "moc",
  "updated": "2026-04-27T12:00:00Z",
  "insights": [
    {
      "id": "moc-001",
      "title": "Tagging reuse unlocks reserved-instance gains",
      "summary": "MOC shows 37% cross-team reuse of tagging policies — standardize naming to unlock reserved-instance coverage gains.",
      "signal": "high",
      "hub": "MOC",
      "ts": "2026-04-27T11:20:00Z"
    },
    {
      "id": "moc-002",
      "title": "Unassociated EIPs in us-east-1",
      "summary": "Top anomaly: unassociated EIPs in us-east-1 projected to add $1.2k/mo; attach or release recommended.",
      "signal": "high",
      "hub": "MOC",
      "ts": "2026-04-27T10:45:00Z"
    },
    {
      "id": "moc-003",
      "title": "GPU workload cost growth",
      "summary": "Forecast indicates 12% QoQ cost growth driven by GPU workloads; evaluate Savings Plans for ml-pools.",
      "signal": "medium",
      "hub": "MOC",
      "ts": "2026-04-27T09:30:00Z"
    }
  ]
}
JSON
      ;;
    cloud-governance)
      cat > "${out}" <<'JSON'
{
  "hub": "Cloud Governance",
  "slug": "cloud-governance",
  "updated": "2026-04-27T12:00:00Z",
  "insights": [
    {
      "id": "cg-001",
      "title": "IAM policy sprawl detected",
      "summary": "Orphaned policies and over-permissioned roles identified across multiple accounts.",
      "signal": "medium",
      "hub": "Cloud Governance",
      "ts": "2026-04-27T11:00:00Z"
    },
    {
      "id": "cg-002",
      "title": "GuardDuty findings trending up",
      "summary": "Medium+ findings increased 22% week-over-week; review top affected resources.",
      "signal": "high",
      "hub": "Cloud Governance",
      "ts": "2026-04-27T10:15:00Z"
    },
    {
      "id": "cg-003",
      "title": "Config compliance drift",
      "summary": "Critical Config rules show 14% non-compliant resources; prioritize remediation.",
      "signal": "medium",
      "hub": "Cloud Governance",
      "ts": "2026-04-27T09:00:00Z"
    }
  ]
}
JSON
      ;;
    *)
      cat > "${out}" <<JSON
{
  "hub": "${slug}",
  "slug": "${slug}",
  "updated": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "insights": []
}
JSON
      ;;
  esac
}

# Generate fallbacks
while IFS= read -r line; do
  slug=$(echo "$line" | jq -r '.slug')
  folder=$(echo "$line" | jq -r '.folder')
  generate_local_fallback "$slug" "$folder"
done < <(jq -c '.hubs[]' "${MANIFEST}")

echo "Hub manifest and fallbacks written to ${OUTPUT_DIR}"
```

Make executable:

```bash
chmod +x /opt/axentx/Costinel/scripts/list-hub-files.sh
```

---

## 2) API route (edge handler) — `/pages/api/top-hub.ts` (or App Router equivalent)

```ts
// /opt/axentx/Costinel/pages/api/top-hub.ts
import type { NextApiRequest, NextApiResponse } from 'next';
import fs from 'fs';
import path from 'path';

const REPO_ROOT = '/opt/axentx/Costinel';
const PUBLIC_HUBS = path.join(REPO_ROOT, 'public/hubs');
const CDN_BASE = 'https://huggingface.co/datasets/axentx/knowledge-rag
