# Costinel / discovery

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

### Highest-value incremental improvement
Add a resilient, zero-runtime-HF-API “Top-Hub Signal” panel to Costinel that surfaces the most-connected hub (e.g., “MOC”) with baked CDN data, robust fallback UI, and no blocking calls during dashboard render.

### Why this ships fast and aligns
- Uses existing knowledge-rag/graph context (top-hub pattern) without new infra.
- Avoids HF API rate limits by baking file list + CDN URLs at build/deploy time.
- Fits Costinel philosophy: “Sense + Signal” — panel signals insight, never executes changes.
- Pure frontend + build-time asset: <2h end-to-end.

---

### Concrete steps (timeboxed)

1. **Create baked asset** (`/public/signals/top-hub.json`)  
   - Contains: `{ hub, score, summary, cdnUrl, updatedAt, relatedDocs[] }`
   - Generate via one-off script (or hand-author once), commit to repo.  
   - Use CDN URLs for any heavy artifacts (e.g., `https://huggingface.co/datasets/.../resolve/main/...`).

2. **Add types** (`src/types/signals.ts`)  
   - Small, strict interface for panel data.

3. **Create panel component** (`src/components/TopHubSignalPanel.tsx`)  
   - Fetch `/signals/top-hub.json` with SWR (or native `fetch` + local fallback).
   - Graceful degraded states: loading → stale data → unavailable.
   - No runtime HF API calls.

4. **Wire into dashboard**  
   - Import and place in cost dashboard sidebar or top-row signal zone.
   - Ensure mobile responsive.

5. **Add tests & lint**  
   - One snapshot/unit test for component states.
   - Type-check.

6. **Verify & ship**  
   - Run dev server, confirm panel renders and falls back when file missing.
   - Commit.

---

### Code snippets

#### 1. Baked signal asset (commit to repo)
`public/signals/top-hub.json`
```json
{
  "hub": "MOC",
  "score": 94.2,
  "summary": "Most-connected hub for cost governance signals. Central node linking RI recommendations, anomaly patterns, and cross-account policy signals.",
  "cdnUrl": "https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub/moc-2026-05-03.json",
  "updatedAt": "2026-05-03T04:20:00Z",
  "relatedDocs": [
    { "title": "RI Coverage Analysis", "path": "/docs/signals/ri-coverage.md" },
    { "title": "Anomaly Taxonomy", "path": "/docs/signals/anomalies.md" },
    { "title": "Cross-Account Policy Patterns", "path": "/docs/signals/policy-patterns.md" }
  ]
}
```

#### 2. Types
`src/types/signals.ts`
```ts
export interface RelatedDoc {
  title: string;
  path: string;
}

export interface TopHubSignal {
  hub: string;
  score: number;
  summary: string;
  cdnUrl: string;
  updatedAt: string;
  relatedDocs: RelatedDoc[];
}
```

#### 3. Panel component
`src/components/TopHubSignalPanel.tsx`
```tsx
import useSWR from 'swr';
import { TopHubSignal } from '../types/signals';
import './TopHubSignalPanel.css';

const fetcher = (url: string) => fetch(url).then((r) => {
  if (!r.ok) throw new Error('Failed to load signal');
  return r.json();
});

export default function TopHubSignalPanel() {
  const { data, error, isLoading } = useSWR<TopHubSignal>(
    '/signals/top-hub.json',
    fetcher,
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      fallbackData: undefined,
    }
  );

  if (isLoading) {
    return (
      <div className="top-hub-panel loading" aria-busy="true">
        <div className="skeleton" />
        <p>Loading signal…</p>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="top-hub-panel unavailable" role="status">
        <p>Signal unavailable</p>
        <small>Check back later or view docs for top hubs.</small>
      </div>
    );
  }

  return (
    <div className="top-hub-panel" role="region" aria-label={`Top hub: ${data.hub}`}>
      <div className="header">
        <span className="badge">Top Hub</span>
        <span className="score" title="Connection score">{data.score.toFixed(1)}</span>
      </div>
      <h3>{data.hub}</h3>
      <p className="summary">{data.summary}</p>

      {data.relatedDocs.length > 0 && (
        <ul className="related-docs" aria-label="Related docs">
          {data.relatedDocs.map((doc) => (
            <li key={doc.path}>
              <a href={doc.path}>{doc.title}</a>
            </li>
          ))}
        </ul>
      )}

      <footer className="footer">
        <small>Updated {new Date(data.updatedAt).toLocaleDateString()}</small>
        {data.cdnUrl && (
          <a href={data.cdnUrl} target="_blank" rel="noopener noreferrer" className="cdn-link">
            View raw
          </a>
        )}
      </footer>
    </div>
  );
}
```

#### 4. Minimal styles
`src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  max-width: 320px;
}

.top-hub-panel .header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 8px;
}

.top-hub-panel .badge {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  color: #0b69ff;
  background: #eef4ff;
  padding: 2px 6px;
  border-radius: 4px;
}

.top-hub-panel .score {
  font-size: 18px;
  font-weight: 700;
  color: #0b69ff;
}

.top-hub-panel h3 {
  margin: 4px 0 8px;
  font-size: 20px;
}

.top-hub-panel .summary {
  margin: 0 0 12px;
  color: #475467;
  font-size: 14px;
  line-height: 1.4;
}

.top-hub-panel .related-docs {
  list-style: none;
  padding: 0;
  margin: 0 0 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.top-hub-panel .related-docs a {
  color: #0b69ff;
  text-decoration: none;
  font-size: 13px;
}

.top-hub-panel .related-docs a:hover {
  text-decoration: underline;
}

.top-hub-panel .footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: #6b7280;
}

.top-hub-panel .cdn-link {
  color: #6b7280;
  text-decoration: none;
}

.top-hub-panel .cdn-link:hover {
  text-decoration: underline;
}

.top-hub-panel .skeleton {
 
