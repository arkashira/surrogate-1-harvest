# Costinel / quality

## Final Decision  
**Ship a read-only “Top-Hub Signal” card on the Costinel ops dashboard** that surfaces the most-connected hub from the existing knowledge-rag graph, shows 1–3 related docs, and refreshes automatically.  

- **Effort**: ~90–120 min  
- **Risk**: Low (read-only, no mutations, graceful fallbacks)  
- **Philosophy**: Sense + Signal — No Execute  

We reconcile Candidate 1 (backend route + live graph query + CDN-bypass for HF datasets) and Candidate 2 (CDN-friendly static JSON + zero backend) by:  
- **Preferring live graph query** (Candidate 1) for correctness and freshness, but **exposing a CDN-friendly static fallback** (Candidate 2) so the card never blocks on heavy ops.  
- **Using a backend route** (`/api/top-hub`) to encapsulate graph access, caching, and fallback logic; frontend remains simple and fast.  
- **Avoiding HF dataset API calls at request time** (Candidate 1 lesson) by relying on pre-generated snapshots via CDN URLs or local static JSON.  

---

## Implementation Plan (concrete, prioritized)

### 1) Backend: `/api/top-hub` (NestJS/Express style)

Purpose: return `{ hub, degree, label?, insight?, relatedDocs[], ts }`.  
Behavior:  
- One lightweight graph query (cached in-memory 300 s).  
- If docs come from Hugging Face datasets, use pre-listed snapshot + CDN URLs (no API calls during request).  
- Graceful fallback to `public/data/knowledge-rag/top-hub.json` if graph unavailable.  
- Never mutate state.

```ts
// src/server/api/top-hub.controller.ts
import { Controller, Get } from '@nestjs/common';
import { KnowledgeRagService } from '../services/knowledge-rag.service';

@Controller('/api/top-hub')
export class TopHubController {
  constructor(private readonly rag: KnowledgeRagService) {}

  @Get()
  async getTopHub() {
    return this.rag.getTopHubSignal();
  }
}
```

```ts
// src/server/services/knowledge-rag.service.ts
import { Injectable } from '@nestjs/common';
import axios from 'axios';
import fs from 'fs';
import path from 'path';

@Injectable()
export class KnowledgeRagService {
  private cache: { data: any; ts: number } | null = null;
  private ttl = 300_000; // 5m

  async getTopHubSignal() {
    if (this.cache && Date.now() - this.cache.ts < this.ttl) {
      return this.cache.data;
    }

    let payload: any = null;

    try {
      // 1) Try live graph
      const hub = await this.queryMostConnectedHub();
      const relatedDocs = await this.fetchRelatedDocs(hub);
      payload = {
        hub: hub.name,
        degree: hub.degree,
        label: hub.label || null,
        insight: hub.insight || null,
        relatedDocs,
        ts: new Date().toISOString(),
      };
    } catch {
      // 2) Fallback to static CDN-friendly JSON
      try {
        const staticPath = path.resolve(
          process.cwd(),
          'public/data/knowledge-rag/top-hub.json'
        );
        const raw = fs.readFileSync(staticPath, 'utf8');
        payload = JSON.parse(raw);
        if (!payload.ts) payload.ts = new Date().toISOString();
      } catch {
        payload = {
          hub: 'N/A',
          degree: 0,
          label: null,
          insight: null,
          relatedDocs: [],
          ts: new Date().toISOString(),
        };
      }
    }

    this.cache = { data: payload, ts: Date.now() };
    return payload;
  }

  private async queryMostConnectedHub() {
    // Replace with your actual graph query (Neo4j / NetworkX / etc.)
    // Example placeholder:
    return { name: 'MOC', degree: 128, label: 'Multi-Org Cost governance', insight: 'Centralize cross-account cost allocation and anomaly detection to reduce noise by ~30%.' };
  }

  private async fetchRelatedDocs(hub: { name: string }) {
    // Use pre-generated HF snapshot via CDN (no API calls at request time)
    const url = `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/snapshots/2026-04-27/top-related-${hub.name}.json`;
    try {
      const { data } = await axios.get(url, { timeout: 4000 });
      return (data || []).slice(0, 3).map((d: any) => ({
        title: d.title || d.slug || 'Untitled',
        snippet: d.snippet || '',
        url: d.url || '',
      }));
    } catch {
      return [
        { title: `${hub.name} overview`, snippet: 'Key insights about the hub.', url: '#' },
      ];
    }
  }
}
```

Static fallback (`public/data/knowledge-rag/top-hub.json`):
```json
{
  "hub": "MOC",
  "label": "Multi-Org Cost governance",
  "connections": 42,
  "insight": "Centralize cross-account cost allocation and anomaly detection to reduce noise by ~30%.",
  "related": [
    { "title": "Cost Anomaly Playbook", "url": "#", "snippet": "Detect, triage, and remediate cost anomalies at scale." },
    { "title": "FinOps Tagging Guide", "url": "#", "snippet": "Standard tags to improve chargeback accuracy." },
    { "title": "MOC Architecture Notes", "url": "#", "snippet": "Key design decisions and ownership model." }
  ],
  "ts": "2026-04-27T00:00:00.000Z"
}
```

---

### 2) Frontend: `TopHubSignalCard`

- Fetches `/api/top-hub`, auto-refreshes every 5 min.  
- Shows loading → data → error states.  
- Read-only; links open in new tab; no execute actions.

```tsx
// src/components/cards/TopHubSignalCard.tsx
'use client';

import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';

interface RelatedDoc {
  title: string;
  snippet: string;
  url: string;
}

interface TopHubPayload {
  hub: string;
  degree: number;
  label?: string | null;
  insight?: string | null;
  relatedDocs: RelatedDoc[];
  ts: string;
}

export default function TopHubSignalCard() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchData = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/top-hub', { cache: 'no-store' });
      const json = await res.json();
      setData(json);
    } catch {
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, 300_000); // 5m
    return () => clearInterval(id);
  }, []);

  if (loading && !data) {
    return (
      <div className="rounded-lg border bg-card p-6">
        <p className="text-sm text-muted-foreground">Loading Top-Hub Signal…</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-6 shadow-sm">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="font-semibold">Top-Hub Signal</h3>
        <button
          onClick={fetchData}
          disabled={loading}
          className="text-xs text-muted-foreground hover:text-foreground"
          aria-label="Refresh"
        >
          <Refresh
