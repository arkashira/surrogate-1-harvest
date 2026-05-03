# Costinel / quality

Candidate 3:
## Implementation Plan — Knowledge-RAG Indexing Pipeline (3–5 days)

**Goal**: Build a robust, automated pipeline that ingests new cost-governance artifacts (PDFs, markdown, spreadsheets) into a vector index, enriches them with entity extraction (hubs, cost-centers, services), and exposes a fast semantic search + “top-hub” API for the frontend. No runtime HuggingFace calls; all embeddings and entity extraction run in CI/nightly and are committed to the repo or a CDN dataset.

---

### 1) High-value scope (3–5 days)
- Ingestion pipeline (Node/Python) that:
  - Watches a repo folder (`knowledge-raw/`) for new files (PDF, md, xlsx).
  - Extracts text (PDF → markdown), parses tables, normalizes filenames.
  - Runs entity extraction (hubs, cost-centers, services) via small local model or rule-based matcher (no HF API at runtime).
  - Produces per-doc JSON with embeddings (via local sentence-transformers) and entity tags.
- Indexing & search:
  - Build a lightweight vector index (e.g., HNSW/FAISS) and persist to repo artifact or CDN dataset.
  - Expose `/api/search` (semantic + keyword hybrid) and `/api/top-hub` (reads baked index).
- Frontend:
  - Add `TopHubSignalPanel` (CDN-first) and a `KnowledgeSearch` component.
  - Graceful fallbacks and skeleton loaders.
- CI/CD:
  - Nightly job that runs ingestion, commits updated index/top-hub JSON to repo or pushes to CDN dataset.

---

### 2) Implementation steps

#### Backend pipeline (Node + Python helpers)
1) Add ingestion script `scripts/ingest-knowledge.js` (or `.py`) that:
   - Reads `knowledge-raw/` and processes new/changed files.
   - Uses `pdf-parse`/`pdfjs` for PDFs, `xlsx` for spreadsheets, and markdown parser for md.
   - Runs entity extraction via small spaCy model or regex dictionary (hubs list).
   - Computes embeddings with `sentence-transformers` (Python) called via subprocess or microservice.
   - Outputs `knowledge-index/{docId}.json` + aggregated `top-hub/latest.json`.

2) Add API routes:
   - `GET /api/search?q=...` — loads index, does hybrid search, returns ranked results.
   - `GET /api/top-hub` — serves baked `top-hub/latest.json`.

#### Frontend
- Create `TopHubSignalPanel` (CDN-first) as in Candidate 1/2.
- Create `KnowledgeSearch` component with debounced search, result list, and doc preview.

#### CI
- Add nightly workflow that:
  - Runs ingestion script.
  - Commits updated `knowledge-index/` and `top-hub/latest.json` to repo (or pushes to HF dataset CDN).

---

### 3) Code snippets (selected)

#### Ingestion pseudo (Node orchestrator + Python embedder)
```js
// scripts/ingest-knowledge.js (Node)
import { globby } from 'globby';
import { extractText } from './lib/pdf.js';
import { parseXlsx } from './lib/xlsx.js';
import { runPython } from './lib/python.js';

async function ingest() {
  const files = await globby('knowledge-raw/**/*.{pdf,md,xlsx}');
  for (const f of files) {
    let text = '';
    if (f.endsWith('.pdf')) text = await extractText(f);
    else if (f.endsWith('.xlsx')) text = await parseXlsx(f);
    else text = await fs.readFile(f, 'utf8');

    const entities = extractEntities(text); // regex/dict based
    const embedding = await runPython('embed.py', text); // returns array
    const doc = { id: slug(f), text, entities, embedding, updatedAt: new Date().toISOString() };
    await fs.writeFile(`knowledge-index/${doc.id}.json`, JSON.stringify(doc));
  }
  await buildTopHub(); // aggregate entity counts -> top-hub/latest.json
}
```

```python
# embed.py (Python)
import sys
import json
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('all-MiniLM-L6-v2')
text = sys.stdin.read()
vec = model.encode(text).tolist()
print(json.dumps(vec))
```

#### API route (Node/Express)
```ts
// src/routes/search.ts
import { loadIndex } from '../lib/search-index';
router.get('/search', async (req, res) => {
  const q = req.query.q as string;
  const results = await loadIndex().search(q);
  res.json(results);
});
```

---

### 4) Deployment checklist (3–5 days)
- [ ] Scaffold ingestion scripts and entity extraction rules.
- [ ] Add embedding generation (local) and index persistence.
- [ ] Implement `/api/search` and `/api/top-hub`.
- [ ] Build frontend components (`TopHubSignalPanel`, `KnowledgeSearch`).
- [ ] Add CI nightly job to run ingestion and commit artifacts.
- [ ] Test end-to-end: add new PDF → nightly run → search and top-hub update.

---

### 5) Why this is the best 3–5 day improvement
- **Scalable ingestion** — automated pipeline for new artifacts, no manual steps.
- **Fast, offline search** — baked embeddings + local index, zero runtime HF API.
- **High signal** — entity extraction surfaces hubs/cost-centers for governance insights.
- **Reuses frontend work** — `TopHubSignalPanel` from <2h plan slots into this larger system.

---

Candidate 4:
## Implementation Plan — Knowledge-RAG Indexing Pipeline (3–5 days)

**Goal**: Build a robust, automated pipeline that ingests new cost-governance artifacts (PDFs, markdown, spreadsheets) into a vector index, enriches them with entity extraction (hubs, cost-centers, services), and exposes a fast semantic search + “top-hub” API for the frontend. No runtime HuggingFace calls; all embeddings and entity extraction run in CI/nightly and are committed to the repo or a CDN dataset.

---

### 1) High-value scope (what ships in 3–5 days)
- Ingestion pipeline (Node + Python) that:
  - Ingests from `knowledge-raw/` (PDF/md/xlsx), extracts text, parses tables.
  - Runs entity extraction (hubs, cost-centers, services) via small local model or rule-based matcher.
  - Computes embeddings locally and produces per-doc JSON + aggregated top-hub JSON.
- Search API + baked top-hub endpoint.
- Frontend: `TopHubSignalPanel` (CDN-first) and `KnowledgeSearch` component.
- CI: nightly job that runs ingestion and commits updated artifacts to repo or pushes to CDN dataset.

---

### 2) Implementation steps

#### Backend pipeline
1) Ingestion script (`scripts/ingest-knowledge`) — same as Candidate 3.
2) API routes: `/api/search` and `/api/top-hub`.
3) Index persistence: repo artifacts or CDN dataset (HF dataset or S3).

#### Frontend
- `TopHubSignalPanel` (CDN-first) and `KnowledgeSearch`.

#### CI
- Nightly workflow to run ingestion and commit/push artifacts.

---

### 3) Code snippets (selected)

#### Ingestion pseudo (same as Candidate 3)
(See Candidate 3 snippets — identical in substance.)

#### API route (same as Candidate 3)
(See Candidate 3 snippets.)

---

### 4) Deployment checklist (3–5 days)
- [ ] Scaffold ingestion scripts and entity extraction rules.
- [ ] Add embedding generation (local) and index persistence.
- [ ] Implement `/api/search` and `/api/top-hub`.
- [ ] Build frontend components (`TopHubSignalPanel`, `KnowledgeSearch`).
- [ ] Add CI nightly job to run ingestion and commit artifacts.
- [ ] Test end-to-end: add new PDF → nightly run → search and top-hub update.

---

### 5) Why this is the best 3–5 day improvement
- **Scalable ingestion** — automated pipeline for new artifacts, no manual steps.
- **Fast, offline search** — baked embeddings + local index, zero runtime HF API.
- **High signal** — entity extraction surfaces hubs/cost-centers for governance insights.
- **Reuses frontend work** — `TopHubSignalPanel
