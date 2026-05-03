# Costinel / frontend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces the most-connected hub (default `MOC`) from a lightweight hub-graph index
- Shows 3 contextual insights from knowledge-rag
- Uses CDN-first pattern: `list_repo_tree` once → embed file list → fetch via CDN (no API calls during render)
- Zero impact on existing dashboard performance (lazy load, client-side hydration)

### Implementation Plan (≤2h)
1. **Create hub-graph index** (5m)  
   Add `/public/knowledge/hub-graph.json` with lightweight graph (hub → edges, last-updated).
2. **Add CDN file list** (5m)  
   Add `/public/knowledge/file-list.json` containing `{date, slug, path}` for available insight docs.
3. **Create TopHubPanel component** (45m)  
   - React component (TypeScript) in `src/components/TopHubPanel.tsx`
   - Fetches `hub-graph.json` + `file-list.json` via CDN (`/resolve/main/` equivalent via relative paths)
   - Picks top hub by edge count (fallback `MOC`)
   - Picks 3 most-recent insight files for that hub
   - Renders card with hub name, connection count, and 3 insights (title + snippet)
   - Skeleton loader + error boundary
4. **Wire into dashboard** (15m)  
   - Import into `src/pages/Dashboard.tsx`
   - Place below main cost KPI row, above service breakdown
   - Responsive grid span (lg:col-span-2)
5. **Styling & polish** (10m)  
   - Use existing design tokens (colors, spacing)
   - Add subtle pulse animation to hub badge
6. **Test & verify** (10m)  
   - Run dev server, confirm CDN fetch, no console errors
   - Verify graceful fallback when files missing

Total: ~90 min (buffer included).

---

### Code snippets

#### 1) `/public/knowledge/hub-graph.json`
```json
{
  "generatedAt": "2026-05-03T03:30:00Z",
  "hubs": {
    "MOC": {
      "label": "MOC",
      "description": "Mission Operations Center",
      "edges": ["COGS", "FinOps", "GRC", "Observability", "Capacity"],
      "edgeCount": 5
    },
    "COGS": {
      "label": "COGS",
      "description": "Cost of Goods Sold",
      "edges": ["MOC", "FinOps"],
      "edgeCount": 2
    },
    "FinOps": {
      "label": "FinOps",
      "description": "Financial Operations",
      "edges": ["MOC", "COGS", "GRC"],
      "edgeCount": 3
    },
    "GRC": {
      "label": "GRC",
      "description": "Governance, Risk, Compliance",
      "edges": ["MOC", "FinOps"],
      "edgeCount": 2
    },
    "Observability": {
      "label": "Observability",
      "description": "Observability & Telemetry",
      "edges": ["MOC"],
      "edgeCount": 1
    },
    "Capacity": {
      "label": "Capacity",
      "description": "Capacity Planning",
      "edges": ["MOC"],
      "edgeCount": 1
    }
  }
}
```

#### 2) `/public/knowledge/file-list.json`
```json
{
  "generatedAt": "2026-05-03T03:30:00Z",
  "files": [
    {
      "date": "2026-04-27",
      "slug": "moc-top-hub-insight",
      "hub": "MOC",
      "path": "knowledge/2026-04-27/moc-top-hub-insight.md",
      "title": "MOC — Top hub insight",
      "snippet": "Review the most-connected hub (MOC) before planning tasks to align signals across operations and cost governance."
    },
    {
      "date": "2026-04-29",
      "slug": "surrogate-1-cdn-bypass",
      "hub": "MOC",
      "path": "knowledge/2026-04-29/surrogate-1-cdn-bypass.md",
      "title": "HF CDN bypass for dataset training",
      "snippet": "Public dataset files at huggingface.co resolve URLs can be fetched via CDN without Authorization — bypasses API rate limits during training."
    },
    {
      "date": "2026-04-29",
      "slug": "lightning-studio-reuse",
      "hub": "MOC",
      "path": "knowledge/2026-04-29/lightning-studio-reuse.md",
      "title": "Lightning Studio reuse saves quota",
      "snippet": "List running studios and reuse them instead of create_ok=True to save ~80hr/mo Lightning quota."
    },
    {
      "date": "2026-04-27",
      "slug": "knowledge-rag-graph",
      "hub": "FinOps",
      "path": "knowledge/2026-04-27/knowledge-rag-graph.md",
      "title": "Knowledge RAG graph patterns",
      "snippet": "After market analysis scripts, run knowledge-rag to query top hub and related docs for contextual insights."
    }
  ]
}
```

#### 3) `src/components/TopHubPanel.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import { Skeleton } from '@/components/ui/skeleton';
import { Alert, AlertDescription } from '@/components/ui/alert';

type HubGraph = {
  generatedAt: string;
  hubs: Record<
    string,
    {
      label: string;
      description: string;
      edges: string[];
      edgeCount: number;
    }
  >;
};

type FileEntry = {
  date: string;
  slug: string;
  hub: string;
  path: string;
  title: string;
  snippet: string;
};

type FileList = {
  generatedAt: string;
  files: FileEntry[];
};

const TopHubPanel: React.FC = () => {
  const [hubGraph, setHubGraph] = useState<HubGraph | null>(null);
  const [fileList, setFileList] = useState<FileList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchJson = async <T,>(url: string): Promise<T> => {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status}`);
      return res.json() as Promise<T>;
    };

    Promise.all([
      fetchJson<HubGraph>('/knowledge/hub-graph.json'),
      fetchJson<FileList>('/knowledge/file-list.json'),
    ])
      .then(([graph, list]) => {
        setHubGraph(graph);
        setFileList(list);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <div className="flex items-center gap-3">
          <Skeleton className="h-10 w-10 rounded-full" />
          <div className="space-y-2 flex-1">
            <Skeleton className="h-5 w-32" />
            <Skeleton className="h-4 w-full" />
          </div>
        </div>
        <div className="mt-4 space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="flex gap-3
