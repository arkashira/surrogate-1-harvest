# Costinel / quality

## Implementation Plan — Costinel “Top-Hub Signal” Card (Read-Only)

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no runtime mutations, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with actionable context and audit trail.

---

### 1) Architecture (read-only)

- **Data source**: static knowledge-rag graph export (`/data/knowledge-rag/top-hubs.json`) produced by ops pipeline.
- **Runtime**: client-side fetch + render (no backend mutation).
- **Safety**: no POST/PUT/DELETE; no inline eval; CSP-friendly.
- **Audit**: render `provenance` + `timestamp` on card; link to full audit view.

---

### 2) File changes

#### A) Add static data file (ops-produced)

`/data/knowledge-rag/top-hubs.json`
```json
{
  "generatedAt": "2026-05-03T04:12:00Z",
  "generatedBy": "knowledge-rag@ops",
  "hubs": [
    {
      "id": "MOC",
      "label": "Mission Operations Center",
      "type": "hub",
      "connections": 312,
      "rank": 1,
      "tags": ["knowledge-rag", "graph", "hub"],
      "summary": "Primary coordination hub for cross-cloud governance workflows.",
      "topSignals": [
        {
          "id": "SIG-001",
          "title": "Reserved Instance coverage gap",
          "severity": "high",
          "impactUSD": 42000,
          "recommendation": "Purchase 3-year convertible RIs for m5.xlarge in us-east-1"
        },
        {
          "id": "SIG-002",
          "title": "Orphaned EBS snapshot volume",
          "severity": "medium",
          "impactUSD": 7200,
          "recommendation": "Apply snapshot lifecycle policy (keep 30d/90d/1yr tiers)"
        }
      ],
      "provenance": {
        "source": "knowledge-rag",
        "runId": "kr-20260503-0412",
        "commit": "a1b2c3d"
      }
    }
  ]
}
```

#### B) Add read-only API route (optional server-side cache)

`/src/routes/api/top-hub/+server.ts`
```ts
import { json } from '@sveltejs/kit';
import fs from 'fs';
import path from 'path';

export async function GET() {
  const filePath = path.resolve('data/knowledge-rag/top-hubs.json');
  const raw = fs.readFileSync(filePath, 'utf8');
  const payload = JSON.parse(raw);

  // strictly read-only; no mutations
  return json({
    hub: payload.hubs[0] || null,
    meta: {
      readOnly: true,
      note: 'Sense + Signal — ไม่ Execute'
    }
  });
}
```

#### C) Add card component

`/src/lib/components/TopHubSignalCard.svelte`
```svelte
<script lang="ts">
  import { onMount } from 'svelte';

  interface TopSignal {
    id: string;
    title: string;
    severity: 'low' | 'medium' | 'high';
    impactUSD: number;
    recommendation: string;
  }

  interface Hub {
    id: string;
    label: string;
    type: string;
    connections: number;
    rank: number;
    tags: string[];
    summary: string;
    topSignals: TopSignal[];
    provenance: {
      source: string;
      runId: string;
      commit: string;
    };
  }

  interface Payload {
    hub: Hub | null;
    meta: { readOnly: boolean; note: string };
  }

  let hub: Hub | null = null;
  let loading = true;
  let error: string | null = null;

  onMount(async () => {
    try {
      const res = await fetch('/api/top-hub');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: Payload = await res.json();
      hub = json.hub;
    } catch (e: any) {
      error = e.message || 'Failed to load top hub';
    } finally {
      loading = false;
    }
  });

  const severityColor = (s: TopSignal['severity']) => {
    switch (s) {
      case 'high': return 'text-red-600 bg-red-50 border-red-200';
      case 'medium': return 'text-amber-600 bg-amber-50 border-amber-200';
      default: return 'text-green-600 bg-green-50 border-green-200';
    }
  };
</script>

<div class="top-hub-card rounded-xl border bg-white p-5 shadow-sm">
  {#if loading}
    <div class="flex items-center gap-3 text-sm text-gray-500">
      <span class="h-3 w-3 animate-pulse rounded-full bg-gray-300" />
      Loading top hub signal…
    </div>
  {:else if error}
    <div class="text-sm text-red-600">{error}</div>
  {:else if hub}
    <header class="mb-4 flex items-start justify-between gap-4">
      <div>
        <h2 class="text-lg font-semibold text-gray-900">{hub.label}</h2>
        <p class="text-sm text-gray-500">{hub.summary}</p>
      </div>
      <span class="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700">
        {hub.connections} connections
      </span>
    </header>

    <section class="mb-4" aria-label="Top signals">
      <h3 class="mb-2 text-sm font-semibold text-gray-700">Top signals</h3>
      <ul class="space-y-2" role="list">
        {#each hub.topSignals as s}
          <li class="rounded-lg border p-3 {severityColor(s)}">
            <div class="flex items-start justify-between gap-2">
              <div>
                <p class="text-sm font-semibold text-gray-900">{s.title}</p>
                <p class="text-xs text-gray-600">{s.recommendation}</p>
              </div>
              <span class="whitespace-nowrap text-xs font-semibold uppercase">
                {s.severity}
              </span>
            </div>
            <p class="mt-1 text-xs text-gray-600">
              Impact: <span class="font-semibold text-gray-900">${s.impactUSD.toLocaleString()}</span>
            </p>
          </li>
        {/each}
      </ul>
    </section>

    <footer class="flex items-center justify-between border-t border-gray-100 pt-3 text-xs text-gray-400">
      <span>Source: {hub.provenance.source}</span>
      <span>Run: {hub.provenance.runId}</span>
      <span>Commit: {hub.provenance.commit}</span>
    </footer>

    <div class="mt-3 text-center">
      <a
        href="/audit/{hub.provenance.runId}"
        class="text-xs text-blue-600 underline hover:text-blue-800"
        target="_blank"
        rel="noopener noreferrer"
      >
        View full audit trail
      </a>
    </div>
  {:else}
    <div class="text-sm text-gray-500">No hub data available.</div>
  {/if}
</div>
```

#### D) Add minimal styles (global or scoped)


