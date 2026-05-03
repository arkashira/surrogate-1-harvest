# Costinel / quality

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & value**  
- Frontend-only, read-only panel that surfaces the most-connected hub (default **“MOC”**) and its actionable proposals from the knowledge graph.  
- Resilient to missing backend: tries live endpoint first, then falls back to bundled cache/sample.  
- Ships in **<2h** with minimal blast radius.  
- **Chosen stack**: Vue 3 + TypeScript + fetch-first resilient data layer.

---

### 1) Data contract (frontend)

`src/types/knowledge-rag.ts`

```ts
export interface Proposal {
  id: string;
  title: string;
  action: string;       // concrete next step (handoff/CTA)
  rationale: string;    // why this proposal matters
  impact: 'high' | 'medium' | 'low';
  tags?: string[];
  href?: string;
}

export interface HubInsight {
  hubId: string;
  label: string;
  description?: string;
  rank?: number;
  updatedAt: string;   // ISO timestamp
  proposals: Proposal[];
}

export interface TopHubResponse {
  topHub: HubInsight | null;
  cached: boolean;
}
```

---

### 2) Resilient data service

`src/services/topHubService.ts`

```ts
import { TopHubResponse, HubInsight, Proposal } from '@/types/knowledge-rag';

const FALLBACK_HUB: HubInsight = {
  hubId: 'MOC',
  label: 'MOC (Most-Connected Hub)',
  description:
    'Multi-cloud optimization cluster — central node for cost governance signals and RI coverage recommendations.',
  rank: 1,
  updatedAt: new Date().toISOString(),
  proposals: [
    {
      id: 'ri-coverage-aws-prod',
      title: 'Increase RI coverage for AWS prod workloads',
      action: 'Run RI recommender and open purchase workflow',
      rationale: 'Current coverage 42% → target 70% yields ~18% YoY savings.',
      impact: 'high',
      tags: ['aws', 'ri', 'coverage'],
    },
    {
      id: 'gcp-committed-use',
      title: 'Shift GCP workloads to committed-use discounts',
      action: 'Generate CUD sizing and execute 12-month commitment',
      rationale: 'Steady-state baseline spend shows 22% savings potential.',
      impact: 'high',
      tags: ['gcp', 'cud'],
    },
    {
      id: 'rightsizing-k8s',
      title: 'Rightsize over-provisioned Kubernetes nodes',
      action: 'Apply vertical/horizontal pod autoscaling policies',
      rationale: 'Observed 35% average CPU idle across node pools.',
      impact: 'medium',
      tags: ['k8s', 'rightsizing'],
    },
  ],
};

async function fetchLive(hubId: string): Promise<HubInsight | null> {
  try {
    const res = await fetch(`/api/knowledge/hubs/${hubId}/signals?limit=10`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as HubInsight;
    // Basic runtime validation (lightweight)
    if (!json || !Array.isArray(json.proposals)) return null;
    return json;
  } catch {
    return null;
  }
}

async function fetchCache(hubId: string): Promise<HubInsight | null> {
  try {
    const res = await fetch(`/cache/top-hub-signals.${hubId}.json`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) throw new Error(`Cache HTTP ${res.status}`);
    const json = (await res.json()) as HubInsight;
    if (!json || !Array.isArray(json.proposals)) return null;
    return json;
  } catch {
    return null;
  }
}

export async function getTopHub(hubId = 'MOC'): Promise<TopHubResponse> {
  const live = await fetchLive(hubId);
  if (live) {
    return { topHub: live, cached: false };
  }

  const cached = await fetchCache(hubId);
  if (cached) {
    return { topHub: cached, cached: true };
  }

  return { topHub: FALLBACK_HUB, cached: true };
}
```

---

### 3) Component contract (props/events)

- **Props**
  - `hubId?: string` — default `"MOC"`
  - `cacheTtlMs?: number` — informational only (used by service if extended)
- **Events**
  - `proposal-click` — emit `Proposal` for parent routing/navigation

---

### 4) UI layout (desktop + mobile)

- Header: hub label + last-updated timestamp + refresh button.
- Description (optional) under header for context.
- Impact pills: **high** (red), **medium** (amber), **low** (gray).
- List of proposals with concise summary, impact pill, and primary action button.
- Empty state: “No active signals” + docs/support link.
- Accessible: semantic headings, `role="status"` for loading, focusable controls.

---

### 5) Implementation

`src/components/TopHubSignalPanel.vue`

```vue
<template>
  <section class="top-hub-panel" aria-labelledby="hub-title">
    <header class="panel-header">
      <div>
        <h2 id="hub-title" class="hub-title">{{ insight?.label ?? '—' }}</h2>
        <p v-if="insight?.description" class="hub-desc">{{ insight.description }}</p>
      </div>
      <div class="meta">
        <span class="updated" aria-live="polite">Updated {{ updatedLabel }}</span>
        <button
          @click="refresh"
          :disabled="loading"
          class="refresh-btn"
          aria-label="Refresh signals"
        >
          ↻
        </button>
      </div>
    </header>

    <div v-if="loading" class="loading" role="status">Loading signals…</div>

    <div v-else-if="error && !hasProposals" class="empty">
      Unable to load live signals. Showing cached data.
      <div v-if="!hasProposals" class="empty-cta">No proposals available.</div>
    </div>

    <div v-else-if="hasProposals" class="proposals-list" role="list">
      <article
        v-for="p in insight!.proposals"
        :key="p.id"
        class="proposal-item"
        role="listitem"
      >
        <div class="proposal-main">
          <h3 class="proposal-title">{{ p.title }}</h3>
          <p class="proposal-rationale">{{ p.rationale }}</p>
          <div class="proposal-meta">
            <span :class="['impact', p.impact]">{{ p.impact }}</span>
            <div class="proposal-tags" v-if="p.tags?.length">
              <span v-for="t in p.tags" :key="t" class="tag">{{ t }}</span>
            </div>
          </div>
        </div>
        <div class="proposal-actions">
          <button @click="selectProposal(p)" class="btn-primary">
            {{ p.action || 'View / Handoff' }}
          </button>
          <a v-if="p.href" :href="p.href" target="_blank" rel="noopener" class="link-secondary">
            Details
          </a>
        </div>
      </article>
    </div>

    <div v-else class="empty">
      No proposals to display.
    </div>

    <div v-if="cached" class="cache-badge" title="Currently showing cached or fallback data">
      Cached data
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, computed, on
