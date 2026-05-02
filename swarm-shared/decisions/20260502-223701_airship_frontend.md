# airship / frontend

### Final Synthesized Implementation Plan  
*(Best parts merged, contradictions resolved for correctness + concrete actionability)*  

---

## Decision  
Ship **`airship discover` frontend orchestrator** that produces **tagged research outputs + CDN-only training manifest** with **one HF API call per run** and **zero runtime API calls during training**.  

---

## Implementation Plan (<2h)  

### 1. Frontend Orchestrator Script (Bash)  
- **Location**: `/opt/axentx/airship/bin/airship-discover-frontend`  
- **Responsibilities**:  
  - Run market analysis (if available).  
  - Query top hub via `knowledge-rag` (fallback to `MOC`).  
  - Generate **CDN-only training manifest** with **one HF API call** (list files → build CDN URLs).  
  - Output tagged research artifacts.  
- **Key correctness fix**: Use **CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) for downloads, avoiding auth/rate limits during training.  

### 2. React UI Component  
- **Location**: `/opt/axentx/airship/frontend/src/components/AirshipDiscover.tsx`  
- **Features**:  
  - Trigger orchestrator via API.  
  - Stream real-time logs.  
  - Display tagged outputs and manifest preview.  

### 3. Frontend API Endpoint  
- **Location**: `/opt/axentx/airship/frontend/src/api/airship.ts`  
- **Endpoint**: `POST /api/airship/discover`  
- **Executes**: `airship-discover-frontend` with proper env (passes `HF_TOKEN` if needed for the single API call).  

### 4. Routing Update  
- **Location**: `/opt/axentx/airship/frontend/src/App.tsx`  
- **Add route**: `/airship/discover`  

---

## Resolved Contradictions  
1. **Script location**: Use `airship-discover-frontend` (Candidate 2) for clarity, not generic `airship-discover`.  
2. **Hub identification**: Query `knowledge-rag` for top hub, but default to `MOC` if unavailable (Candidate 2) — more robust than Candidate 1’s hardcoded approach.  
3. **HF API usage**: Exactly **one API call per run** to list files (Candidate 2), then embed paths in manifest. Candidate 1’s “HF CDN bypass” is implemented correctly here via **CDN URLs** (no auth needed for public files).  
4. **Training-time API calls**: **Zero** — manifest includes CDN URLs; training script reads local file list or URLs directly.  

---

## Final Code Snippets  

### 1. Frontend Orchestrator Script  
```bash
#!/usr/bin/env bash
# /opt/axentx/airship/bin/airship-discover-frontend
# Airship Frontend Orchestrator
# Produces tagged research outputs + CDN-only training manifest
# One HF API call per run, zero runtime API calls during training

set -euo pipefail
SHELL=/bin/bash

# Config
OUTPUT_DIR="/opt/axentx/airship/outputs"
MANIFEST_FILE="${OUTPUT_DIR}/training-manifest.json"
LOG_FILE="${OUTPUT_DIR}/airship-discover.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p "${OUTPUT_DIR}"

log() {
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*" | tee -a "${LOG_FILE}"
}

log "=== Airship Frontend Discover Started ==="

# Step 1: Market analysis (optional)
log "Step 1: Running market analysis (if available)..."
if command -v granite-business-research.sh >/dev/null 2>&1; then
    granite-business-research.sh >> "${LOG_FILE}" 2>&1 || log "Market analysis failed, continuing..."
else
    log "granite-business-research.sh not found, skipping..."
fi

# Step 2: Identify top hub
log "Step 2: Identifying top hub..."
TOP_HUB="MOC"
if command -v knowledge-rag >/dev/null 2>&1; then
    TOP_HUB=$(knowledge-rag --query "top hub" --format json 2>/dev/null | jq -r '.top_hub // "MOC"' || echo "MOC")
fi
log "Top hub: ${TOP_HUB}"

# Step 3: Generate CDN-only training manifest (single HF API call)
log "Step 3: Generating CDN-only training manifest..."
HF_REPO="axentx/surrogate-training-data"
HF_FOLDER="batches/mirror-merged/$(date -u +"%Y-%m-%d")"

# Single API call to list files
FILE_LIST=$(curl -s -H "Authorization: Bearer ${HF_TOKEN:-}" \
    "https://huggingface.co/api/datasets/${HF_REPO}/tree?path=${HF_FOLDER}&recursive=false" || echo "[]")

# Build manifest with CDN URLs
MANIFEST=$(jq -n \
    --arg timestamp "${TIMESTAMP}" \
    --arg top_hub "${TOP_HUB}" \
    --arg folder "${HF_FOLDER}" \
    --arg repo "${HF_REPO}" \
    --argjson files "$(echo "${FILE_LIST}" | jq '[.[]?.path // empty]')" \
    '{
        "timestamp": $timestamp,
        "top_hub": $top_hub,
        "hf_folder": $folder,
        "cdn_base": "https://huggingface.co/datasets/\($repo)/resolve/main",
        "files": $files,
        "cdn_urls": [$files[] | "\($cdn_base)/\($folder)/\($.)"],
        "tags": ["#business-research", "#knowledge-rag", "#graph", "#cdn-bypass", "#training-manifest"]
    }')

echo "${MANIFEST}" > "${MANIFEST_FILE}"
log "Manifest saved to: ${MANIFEST_FILE}"

# Step 4: Tagged research output
cat > "${OUTPUT_DIR}/research-output.json" << EOF
{
    "timestamp": "${TIMESTAMP}",
    "top_hub": "${TOP_HUB}",
    "tags": ["#business-research", "#knowledge-rag", "#graph", "#hub-insight"],
    "insights": {
        "most_connected_hub": "${TOP_HUB}",
        "research_phase": "frontend-orchestrator",
        "manifest_location": "${MANIFEST_FILE}"
    }
}
EOF

log "=== Airship Frontend Discover Completed ==="

# API-friendly summary
jq -n \
    --arg status "success" \
    --arg timestamp "${TIMESTAMP}" \
    --arg top_hub "${TOP_HUB}" \
    --arg manifest "${MANIFEST_FILE}" \
    --arg log "${LOG_FILE}" \
    '{status: $status, timestamp: $timestamp, top_hub: $top_hub, manifest: $manifest, log: $log}'
```

---

### 2. React Component (Skeleton)  
```tsx
// /opt/axentx/airship/frontend/src/components/AirshipDiscover.tsx
import React, { useState } from 'react';
import { runAirshipDiscover } from '../api/airship';

export const AirshipDiscover: React.FC = () => {
  const [logs, setLogs] = useState<string[]>([]);
  const [manifest, setManifest] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const handleRun = async () => {
    setLoading(true);
    setLogs([]);
    try {
      const result = await runAirshipDiscover();
      setManifest(result);
      // In real impl, stream logs via WebSocket or poll log file
      setLogs(prev => [...prev, `Completed at ${result.timestamp}`]);
    } catch (err) {
      setLogs(prev => [...prev, `Error: ${err}`]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <button onClick={handleRun} disabled={loading}>
        {loading ? 'Running...' : 'Run Airship Discover'}
      </button>
      <div>
        <h3>Logs</h3>
        <pre>{logs.join('\n')}</pre>
      </div>
      {manifest && (
        <div>
          <h3>Manifest Preview</h3>

