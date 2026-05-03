# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**:  
Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals. Read-only. Zero backend changes.

**Timebox**: ≤2h  
**Tags**: #knowledge-rag #graph #hub #Costinel

---

### 1) What to ship
- One read-only card component:
  - Title: **“Top Hub Signal”**
  - Hub name + short description (from graph metadata)
  - 3 contextual signals (edges/documents) with relevance score and one-line insight
  - Loading, empty, and error states
- Static mock data (JSON) mimicking the graph response so it works without backend.
- Optional lightweight fetch from `/data/knowledge-rag/top-hubs.json` when available; fallback to bundled mock.
- Drop-in placement: dashboard sidebar or top-of-page signal rail.

---

### 2) Files to touch (paths relative to `/opt/axentx/Costinel`)
- `src/components/cards/TopHubSignalCard.tsx` (new)
- `src/mocks/topHubMock.ts` (new)
- `src/types/knowledgeGraph.ts` (add minimal types)
- `src/pages/Dashboard.tsx` (import + mount card)
- `public/data/knowledge-rag/top-hubs.json` (optional static file)

---

### 3) Implementation steps (minute-by-minute)

#### 0–5m — Add types (`src/types/knowledgeGraph.ts`)
```ts
export interface KnowledgeHub {
  id: string;
  label: string;
  description?: string;
  degree: number;
  tags?: string[];
}

export interface KnowledgeSignal {
  id: string;
  title: string;
  snippet: string;
  relevance: number; // 0–1
  edgeType?: string; // e.g. "mentions", "derives_from"
}

export interface TopHubPayload {
  hub: KnowledgeHub;
  signals: KnowledgeSignal[];
  generatedAt: string;
}
```

#### 5–10m — Create mock data (`src/mocks/topHubMock.ts`)
```ts
import { TopHubPayload } from '@/types/knowledgeGraph';

export const topHubMock: TopHubPayload = {
  hub: {
    id: 'MOC',
    label: 'MOC (Management of Change)',
    description: 'Central policy & procedure hub for change governance across cloud environments.',
    degree: 42,
    tags: ['governance', 'change-management', 'policy'],
  },
  signals: [
    {
      id: 's1',
      title: 'RI coverage gaps linked to MOC delays',
      snippet: 'Pending MOC approvals correlate with 18% lower reserved instance coverage in production.',
      relevance: 0.92,
      edgeType: 'correlates_with',
    },
    {
      id: 's2',
      title: 'Tagging enforcement playbook',
      snippet: 'MOC references the standardized tagging playbook used by FinOps to attribute orphaned resources.',
      relevance: 0.87,
      edgeType: 'references',
    },
    {
      id: 's3',
      title: 'Exception trend: emergency change spikes',
      snippet: 'Emergency change requests bypassing MOC increased 34% QoQ — watch for cost drift.',
      relevance: 0.81,
      edgeType: 'detects',
    },
  ],
  generatedAt: new Date().toISOString(),
};
```

#### 10–40m — Create card component (`src/components/cards/TopHubSignalCard.tsx`)
```tsx
import React, { useEffect, useState } from 'react';
import { TopHubPayload } from '@/types/knowledgeGraph';
import { CircleInfo } from 'lucide-react';
import { topHubMock } from '@/mocks/topHubMock';

interface TopHubSignalCardProps {
  preferStatic?: boolean; // for explicit control in tests/stories
}

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({
  preferStatic = false,
}) => {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (preferStatic) {
      setData(topHubMock);
      setLoading(false);
      return;
    }

    let mounted = true;
    setLoading(true);

    // Try public JSON first; fallback to mock
    fetch('/data/knowledge-rag/top-hubs.json', { cache: 'no-store' })
      .then(async (res) => {
        if (!res.ok) throw new Error('Static file not available');
        return res.json() as Promise<TopHubPayload>;
      })
      .then((json) => {
        if (!mounted) return;
        // Basic shape validation
        if (!json?.hub || !Array.isArray(json.signals)) throw new Error('Invalid payload shape');
        setData(json);
      })
      .catch(() => {
        if (!mounted) return;
        // Graceful fallback
        setData(topHubMock);
      })
      .finally(() => {
        if (!mounted) return;
        setLoading(false);
        setError(null);
      });

    return () => {
      mounted = false;
    };
  }, [preferStatic]);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <div className="flex items-center gap-2 mb-3">
          <CircleInfo className="h-4 w-4 text-muted-foreground animate-pulse" />
          <div className="h-4 w-32 bg-muted rounded animate-pulse" />
        </div>
        <div className="space-y-2">
          <div className="h-3 w-full bg-muted rounded animate-pulse" />
          <div className="h-3 w-5/6 bg-muted rounded animate-pulse" />
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
        Top hub signal unavailable.
      </div>
    );
  }

  const { hub, signals } = data;

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2">
          <CircleInfo className="h-4 w-4 text-primary mt-0.5" />
          <div>
            <h3 className="font-semibold text-sm leading-tight">{hub.label}</h3>
            <p className="text-xs text-muted-foreground">{hub.degree} connections</p>
          </div>
        </div>
      </div>

      {hub.description && (
        <p className="text-xs text-muted-foreground mb-3 line-clamp-2">{hub.description}</p>
      )}

      <div className="space-y-2">
        {signals.map((s) => (
          <div
            key={s.id}
            className="p-2 rounded bg-muted/40 border border-border/40 text-xs"
          >
            <div className="flex items-center justify-between mb-1">
              <span className="font-medium text-foreground truncate">{s.title}</span>
              <span className="text-muted-foreground text-[10px] flex-shrink-0 ml-1">
                {Math.round(s.relevance * 100)}%
              </span>
            </div>
            <p className="text-muted-foreground line-clamp-2">{s.snippet}</p>
            <div className="mt-1 flex items-center justify-between">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                {s.edgeType || 'related'}
              </span>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-3 pt-3 border-t border-border/40 text-[1
