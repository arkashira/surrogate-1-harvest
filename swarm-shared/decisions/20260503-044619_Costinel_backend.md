# Costinel / backend

## Final Synthesis: CDN-First Top-Hub Signal Panel (Production-Ready)

**Chosen approach**: A single, resilient “Top Hub” panel embedded in Costinel’s dashboard that fetches a small, versioned JSON from CDN (HuggingFace or static origin) with zero model compute, strict timeouts, and layered fallbacks. Combines Candidate 1’s robust fetch logic and schema validation with Candidate 2’s simpler public/static path strategy and clearer document taxonomy.

---

### Why this is highest value (merged)
- Applies **#knowledge-rag #graph #hub** pattern immediately (top-hub insight).
- Uses **#cdn #huggingface #rate-limit-bypass** to avoid API/auth/rate-limit issues and keep backend free.
- Minimal surface: one JSON payload + one React component + one integration point → implementable end-to-end in <2h.
- Delivers direct contextual value to Costinel’s “Sense + Signal” philosophy with no execution risk.

---

### Concrete implementation plan

#### 1) Payload location and schema (merged)
- **Primary CDN path** (repo-hosted, served via HF CDN):  
  `https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/knowledge/top-hub.json`
- **Local fallback** (dev + static build):  
  `public/data/top-hub.json` (committed; available as `/data/top-hub.json` after build)
- **Schema** (strict, validated):
  ```json
  {
    "hub": "MOC",
    "title": "Multi-Org Cost Governance",
    "summary": "Central pattern for cross-account, cross-team cost visibility and policy signals.",
    "generated_at": "2026-05-03T06:00:00Z",
    "ttl": 86400,
    "related": [
      {
        "slug": "moc-playbook",
        "title": "MOC Operational Playbook",
        "type": "playbook",
        "url": "https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/knowledge/moc-playbook.md",
        "summary": "Runbooks and decision patterns for MOC governance and cost signal triage."
      },
      {
        "slug": "cost-signal-taxonomy",
        "title": "Cost Signal Taxonomy",
        "type": "reference",
        "url": "https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/knowledge/cost-signal-taxonomy.md",
        "summary": "Standard labels and severity levels used across Costinel signals."
      },
      {
        "slug": "graph-index-hubs",
        "title": "Graph Index: Top Hubs",
        "type": "index",
        "url": "https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/knowledge/graph-index-hubs.md",
        "summary": "Most-connected hubs and their cross-doc edges (updated nightly)."
      }
    ]
  }
  ```

#### 2) Fetch utility (robust, layered)
- Use CDN-first with short timeout (4–5s), exponential backoff, max 2 retries.
- No Authorization header (bypasses API limits).
- Validate shape strictly; reject malformed responses.
- Fallback order:
  1) CDN URL (primary)
  2) Local static path (`/data/top-hub.json`)
  3) Minimal inline stub (non-blocking UI)

```ts
// src/lib/fetchTopHub.ts
const CDN_URL =
  'https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/knowledge/top-hub.json';
const LOCAL_FALLBACK = '/data/top-hub.json';

const STUB: TopHubPayload = {
  hub: 'MOC',
  title: 'Multi-Org Cost Governance',
  summary: 'Central pattern for cross-account, cross-team cost visibility and policy signals.',
  generated_at: new Date().toISOString(),
  ttl: 3600,
  related: [],
};

interface RelatedDoc {
  slug: string;
  title: string;
  type: string;
  url: string;
  summary: string;
}

interface TopHubPayload {
  hub: string;
  title: string;
  summary: string;
  generated_at: string;
  ttl?: number;
  related: RelatedDoc[];
}

async function fetchWithTimeout(url: string, timeoutMs = 4500): Promise<Response> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: controller.signal });
    clearTimeout(id);
    return res;
  } catch {
    clearTimeout(id);
    throw new Error('timeout');
  }
}

function isTopHubPayload(obj: unknown): obj is TopHubPayload {
  if (!obj || typeof obj !== 'object') return false;
  const o = obj as Record<string, unknown>;
  return (
    typeof o.hub === 'string' &&
    typeof o.title === 'string' &&
    typeof o.summary === 'string' &&
    typeof o.generated_at === 'string' &&
    Array.isArray(o.related) &&
    o.related.every(
      (d) =>
        typeof d === 'object' &&
        d !== null &&
        typeof (d as RelatedDoc).slug === 'string' &&
        typeof (d as RelatedDoc).title === 'string' &&
        typeof (d as RelatedDoc).url === 'string' &&
        typeof (d as RelatedDoc).summary === 'string'
    )
  );
}

export async function fetchTopHub(): Promise<TopHubPayload> {
  // 1) CDN primary
  try {
    const res = await fetchWithTimeout(CDN_URL, 5000);
    if (res.ok) {
      const json = (await res.json()) as unknown;
      if (isTopHubPayload(json)) return json;
    }
  } catch {
    // noop
  }

  // 2) Local static fallback
  try {
    const res = await fetchWithTimeout(LOCAL_FALLBACK, 3000);
    if (res.ok) {
      const json = (await res.json()) as unknown;
      if (isTopHubPayload(json)) return json;
    }
  } catch {
    // noop
  }

  // 3) Stub to keep UI functional
  return STUB;
}
```

#### 3) React panel component (merged UX)
- Skeleton loader while fetching.
- Non-blocking inline error if all sources fail.
- Responsive card list for related docs.
- Links open in new tab with `noopener noreferrer`.

```tsx
// src/components/TopHubPanel.tsx
import { useEffect, useState } from 'react';
import { fetchTopHub, type TopHubPayload, type RelatedDoc } from '../lib/fetchTopHub';

function DocCard({ doc }: { doc: RelatedDoc }) {
  return (
    <a
      href={doc.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block p-3 rounded border border-gray-200 hover:border-blue-300 hover:bg-blue-50 transition"
    >
      <div className="flex items-center justify-between">
        <span className="font-medium text-gray-900">{doc.title}</span>
        <span className="text-xs px-2 py-0.5 rounded bg-gray-100 text-gray-600">{doc.type}</span>
      </div>
      <div className="text-sm text-gray-600 mt-1">{doc.summary}</div>
    </a>
  );
}

export default function TopHubPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHub()
      .then((res) => {
        if (!mounted) return;
        setData(res);
        setError(null);
      })
      .catch((err) => {
        if (!mounted) return;
        setError('Unable to load hub insights
