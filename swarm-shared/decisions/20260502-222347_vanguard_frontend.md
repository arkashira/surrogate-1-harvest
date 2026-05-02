# vanguard / frontend

## 1. Diagnosis
- No canonical frontend discovery entrypoint to surface top-hub insights (e.g., MOC) before planning — violates `#knowledge-rag #graph #hub` and forces ad-hoc exploration.
- Missing CDN-bypass file-list strategy for HF datasets in frontend flows; any future surrogate-1 training previews will hit API rate limits.
- No persistent “run knowledge-rag after market analysis” UX hook — breaks `#business-research #knowledge-rag #graph` pattern.
- No visible guardrails for HF CDN usage (public resolve URLs) in frontend dataset previews — future training iterations will be throttled.
- Absence of a lightweight, reusable top-hub insight component increases cognitive load when planning frontend tasks.

## 2. Proposed change
Add a minimal, high-value frontend module:  
`/opt/axentx/vanguard/src/features/knowledgeHub/TopHubInsightPanel.tsx` + route + lazy-loaded entry in the main layout.  
Scope: single panel that fetches and displays the most-connected hub (e.g., MOC) and related docs; exposes “Run knowledge-rag” action that can be wired to market-analysis flows.  
Also add a small util for CDN-bypass file-list resolution to be reused by dataset preview components.

## 3. Implementation
Create files (TypeScript/React). Adjust imports/paths to match your actual router and state layer.

```bash
mkdir -p /opt/axentx/vanguard/src/features/knowledgeHub
mkdir -p /opt/axentx/vanguard/src/lib/hf
```

`/opt/axentx/vanguard/src/lib/hf/cdn.ts`
```ts
// HF CDN-bypass helpers (no Authorization header; uses resolve/main/)
export const HF_CDN_BASE = 'https://huggingface.co/datasets';

export function resolveDatasetFile(repo: string, path: string): string {
  return `${HF_CDN_BASE}/${repo}/resolve/main/${path}`;
}

// Fetch a precomputed file-list JSON (produced by Mac orchestration script)
// Example expected JSON: ["folder/file1.parquet", "folder/file2.parquet"]
export async function fetchFileList(
  repo: string,
  listPath: string
): Promise<string[]> {
  const url = resolveDatasetFile(repo, listPath);
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Failed to fetch file-list: ${res.status}`);
  return res.json();
}
```

`/opt/axentx/vanguard/src/features/knowledgeHub/useTopHubInsight.ts`
```ts
import { useEffect, useState } from 'react';

export interface HubDoc {
  id: string;
  title: string;
  score: number;
  snippet: string;
  route?: string;
}

export interface TopHub {
  key: string;
  label: string;
  centrality: number;
  topDocs: HubDoc[];
}

// Lightweight adapter — replace endpoint with your internal RAG/graph API when available.
async function fetchTopHub(): Promise<TopHub> {
  // Placeholder: simulate fetching most-connected hub (e.g., MOC)
  // In production, call your knowledge-rag service (GET /api/graph/hubs/top)
  await new Promise((r) => setTimeout(r, 200));
  return {
    key: 'MOC',
    label: 'MOC — Mission Operations Center',
    centrality: 0.92,
    topDocs: [
      { id: 'moc-ops', title: 'MOC Operations Playbook', score: 0.95, snippet: 'Runbooks and escalation paths for mission operations.', route: '/docs/moc/ops' },
      { id: 'moc-arch', title: 'MOC Architecture Overview', score: 0.89, snippet: 'System boundaries and interfaces for MOC.', route: '/docs/moc/arch' },
    ],
  };
}

export function useTopHubInsight() {
  const [hub, setHub] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchTopHub();
      setHub(result);
    } catch (err) {
      setError(err as Error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { hub, loading, error, refresh };
}
```

`/opt/axentx/vanguard/src/features/knowledgeHub/TopHubInsightPanel.tsx`
```tsx
import React from 'react';
import { useTopHubInsight } from './useTopHubInsight';
import { Link } from 'react-router-dom';

export const TopHubInsightPanel: React.FC = () => {
  const { hub, loading, error, refresh } = useTopHubInsight();

  const handleRunRag = () => {
    // Wire this to your market-analysis + knowledge-rag orchestration.
    // Example: open a side panel, trigger webhook, or navigate to a RAG run view.
    // For now, refresh and simulate run.
    refresh();
    // Optionally: window.open('/rag/run?hub=MOC', '_blank');
  };

  if (loading && !hub) {
    return (
      <div className="p-4 border rounded bg-gray-50 text-sm text-gray-600">
        Loading top-hub insights...
      </div>
    );
  }

  return (
    <div className="p-4 border rounded bg-white shadow-sm">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold text-gray-900">Top Hub Insight</h3>
        <button
          onClick={handleRunRag}
          className="text-xs px-2 py-1 rounded border border-gray-300 hover:bg-gray-50"
          title="Run knowledge-rag for contextual insights"
        >
          Run RAG
        </button>
      </div>

      {error && <p className="text-red-600 text-sm mb-2">{error.message}</p>}

      {hub && (
        <>
          <div className="mb-2">
            <span className="font-medium text-blue-700">{hub.label}</span>
            <span className="ml-2 text-xs text-gray-500">centrality: {hub.centrality.toFixed(2)}</span>
          </div>

          <ul className="space-y-2">
            {hub.topDocs.map((doc) => (
              <li key={doc.id} className="text-sm">
                <Link to={doc.route || '#'} className="hover:underline text-gray-800">
                  {doc.title}
                </Link>
                <p className="text-gray-500 text-xs mt-0.5">{doc.snippet}</p>
              </li>
            ))}
          </ul>

          <p className="mt-3 text-xs text-gray-400">
            Tip: Review the most-connected hub before planning tasks (pattern: top-hub doc insight).
          </p>
        </>
      )}
    </div>
  );
};
```

`/opt/axentx/vanguard/src/features/knowledgeHub/index.ts`
```ts
export { TopHubInsightPanel } from './TopHubInsightPanel';
```

Wire into your layout (example for a right-sidebar or main column):
```tsx
// Example: in your main layout or dashboard page
import { TopHubInsightPanel } from '@/features/knowledgeHub';

export default function DashboardLayout() {
  return (
    <div className="flex gap-6">
      <main className="flex-1">{/* existing content */}</main>
      <aside className="w-80">
        <TopHubInsightPanel />
        {/* other panels */}
      </aside>
    </div>
  );
}
```

If you want a route (e.g., `/hub/top`), add a lazy-loaded route in your router config.

## 4. Verification
- Load the frontend and confirm the panel renders with the placeholder MOC hub and two docs.

