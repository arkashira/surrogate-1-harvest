# Costinel / quality

Candidate 3:
## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data (zero runtime HF API). Deterministic, cacheable, and safe for production.

---

### 1) Architecture (CDN-first, deterministic)

```
┌─────────────────────┐
│  Build/Deploy step  │  (GitHub Action or pre-deploy)
│  generate-top-hub   │
└─────────┬───────────┘
          │
          ▼
  public/signals/top-hub.json
          │
          ▼
┌─────────────────────┐
│  Costinel frontend  │  fetch("/signals/top-hub.json")
│  (static/CDN)       │  → render panel (non-blocking)
└─────────────────────┘
```

- **Zero runtime HF API**: file is generated at build/deploy time and served via CDN/public path.
- **Non-blocking**: panel loads async; failure is silent (no UI crash).
- **Deterministic**: same slug → same repo via hash-slug routing if/when multi-repo siblings are used (follows HF commit-cap pattern).
- **Cacheable**: long `Cache-Control` on CDN; versioned filename or query string for invalidation.

---

### 2) Data contract (public/signals/top-hub.json)

```json
{
  "hub": "MOC",
  "label": "MOC",
  "description": "Most-connected hub for cost governance signals",
  "tags": ["knowledge-rag", "graph", "hub"],
  "updated": "2026-05-03T04:02:46Z",
  "ttl": 86400,
  "links": {
    "hub": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/moc/summary.json",
    "dashboard": "/signals/hubs/moc"
  }
}
```

- Minimal surface. `ttl` lets frontend know staleness tolerance.
- `links.hub` points to CDN file (bypass HF API) for deeper drill-down.

---

### 3) Generation script (run at build/deploy)

`scripts/generate-top-hub.js` (Node, zero deps beyond fs/https)

```js
#!/usr/bin/env node
/**
 * Generate public/signals/top-hub.json at build time.
 * Uses CDN URLs only (no HF API auth

---

Candidate 4:
## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data (zero runtime HF API). Deterministic, cacheable, and safe for production.

---

### 1) Architecture (CDN-first, deterministic)

```
┌─────────────────────┐
│  Build/Deploy step  │  (GitHub Action or pre-deploy)
│  generate-top-hub   │
└─────────┬───────────┘
          │
          ▼
  public/signals/top-hub.json
          │
          ▼
┌─────────────────────┐
│  Costinel frontend  │  fetch("/signals/top-hub.json")
│  (static/CDN)       │  → render panel (non-blocking)
└─────────────────────┘
```

- **Zero runtime HF API**: file is generated at build/deploy time and served via CDN/public path.
- **Non-blocking**: panel loads async; failure is silent (no UI crash).
- **Deterministic**: same slug → same repo via hash-slug routing if/when multi-repo siblings are used (follows HF commit-cap pattern).
- **Cacheable**: long `Cache-Control` on CDN; versioned filename or query string for invalidation.

---

### 2) Data contract (public/signals/top-hub.json)

```json
{
  "hub": "MOC",
  "label": "MOC",
  "description": "Most-connected hub for cost governance signals",
  "tags": ["knowledge-rag", "graph", "hub"],
  "updated": "2026-05-03T04:02:46Z",
  "ttl": 86400,
  "links": {
    "hub": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/moc/summary.json",
    "dashboard": "/signals/hubs/moc"
  }
}
```

- Minimal surface. `ttl` lets frontend know staleness tolerance.
- `links.hub` points to CDN file (bypass HF API) for deeper drill-down.

---

### 3) Generation script (run at build/deploy)

`scripts/generate-top-hub.js` (Node, zero deps beyond fs/https)

```js
#!/usr/bin/env node
/**
 * Generate public/signals/top-hub.json at build time.
 * Uses CDN URLs only (no HF API auth

---

Candidate 5:
## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data (zero runtime HF API). Deterministic, cacheable, and safe for production.

---

### 1) Architecture (CDN-first, deterministic)

```
┌─────────────────────┐
│  Build/Deploy step  │  (GitHub Action or pre-deploy)
│  generate-top-hub   │
└─────────┬───────────┘
          │
          ▼
  public/signals/top-hub.json
          │
          ▼
┌─────────────────────┐
│  Costinel frontend  │  fetch("/signals/top-hub.json")
│  (static/CDN)       │  → render panel (non-blocking)
└─────────────────────┘
```

- **Zero runtime HF API**: file is generated at build/deploy time and served via CDN/public path.
- **Non-blocking**: panel loads async; failure is silent (no UI crash).
- **Deterministic**: same slug → same repo via hash-slug routing if/when multi-repo siblings are used (follows HF commit-cap pattern).
- **Cacheable**: long `Cache-Control` on CDN; versioned filename or query string for invalidation.

---

### 2) Data contract (public/signals/top-hub.json)

```json
{
  "hub": "MOC",
  "label": "MOC",
  "description": "Most-connected hub for cost governance signals",
  "tags": ["knowledge-rag", "graph", "hub"],
  "updated": "2026-05-03T04:02:46Z",
  "ttl": 86400,
  "links": {
    "hub": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/moc/summary.json",
    "dashboard": "/signals/hubs/moc"
  }
}
```

- Minimal surface. `ttl` lets frontend know staleness tolerance.
- `links.hub` points to CDN file (bypass HF API) for deeper drill-down.

---

### 3) Generation script (run at build/deploy)

`scripts/generate-top-hub.js` (Node, zero deps beyond fs/https)

```js
#!/usr/bin/env node
/**
 * Generate public/signals/top-hub.json at build time.
 * Uses CDN URLs only (no HF API auth

---

Candidate 6:
## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data (zero runtime HF API). Deterministic, cacheable, and safe for production.

---

### 1) Architecture (CDN-first, deterministic)

```
┌─────────────────────┐
│  Build/Deploy step  │  (GitHub Action or pre-deploy)
│  generate-top-hub
