# Costinel / frontend

## Final Implementation Plan — Costinel Discovery (Frontend + CLI)

**Single source of truth**: deterministic, audit-ready, **Sense + Signal — No Execution**.  
Ship both a frontend `DiscoveryView` (read-only signals) and a CLI `discovery run --env <env>` (manifest + top-hub snapshot) that share the same schema and deterministic fixtures.

---

### Core Principles (resolve contradictions)
- **No execution, ever** — frontend is read-only; CLI writes only local JSON artifacts.
- **Deterministic by default** — mock fixtures drive dev/test; prod replaces fetches with real API/CDN calls without changing signatures.
- **Shared schema** — frontend and CLI use identical TypeScript types (frontend) / JSON schema (CLI) so audits and tests are consistent.
- **2h scope hard limit** — frontend: route + store + view + mocks; CLI: runner + manifest builder + top-hub snapshot + smoke test.

---

### 1) Shared Types (single file, frontend)
`/src/types/discovery.ts`
```ts
export interface DiscoveryResource {
  id: string;
  name: string;
  type: string;
  region: string;
  accountId: string;
  tags: Record<string, string>;
  cost?: { monthly: number; currency: string };
  metadata: Record<string, unknown>;
}

export interface DiscoverySignal {
  id: string;
  type: 'inefficiency' | 'anomaly' | 'opportunity' | 'risk';
  severity: 'low' | 'medium' | 'high' | 'critical';
  title: string;
  description: string;
  context: Record<string, unknown>;
  recommendation: string;
  resourceIds: string[];
}

export interface DiscoveryManifest {
  env: string;
  generatedAt: string;
  version: string;
  summary: {
    totalResources: number;
    totalMonthlyCost: number;
    servicesCount: number;
    regionsCount: number;
  };
  resources: DiscoveryResource[];
  signals: DiscoverySignal[];
}

export interface TopHubInsight {
  hubId: string;
  label: string;
  type: 'MOC' | 'account' | 'service' | 'region';
  connections: number;
  centrality: number;
  tags: string[];
  summary: string;
  relatedResources: Array<{ id: string; type: string; relation: string }>;
  signals: Array<{ id: string; severity: string; title: string }>;
}
```

CLI equivalent (JSON Schema) — keep in `cli/schema/` for validation:
- `manifest.json` must validate against a generated schema matching `DiscoveryManifest`.
- `top-hub.json` must validate against a schema matching `TopHubInsight`.

---

### 2) Frontend — read-only discovery view

#### Route (`/src/router/index.ts`)
```ts
import { createRouter, createWebHistory } from 'vue-router';
import DiscoveryView from '@/views/DiscoveryView.vue';

const routes = [
  // ... existing
  {
    path: '/discovery',
    name: 'Discovery',
    component: DiscoveryView,
    meta: { title: 'Discovery — Costinel', requiresAuth: true }
  }
];

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
});

export default router;
```

#### Store (`/src/stores/discovery.ts`)
```ts
import { defineStore } from 'pinia';
import type { DiscoveryManifest, TopHubInsight } from '@/types/discovery';

interface DiscoveryState {
  manifests: Record<string, DiscoveryManifest>;
  topHubInsights: Record<string, TopHubInsight>;
  loading: boolean;
  error: string | null;
  lastRunAt: string | null;
}

export const useDiscoveryStore = defineStore('discovery', {
  state: (): DiscoveryState => ({
    manifests: {},
    topHubInsights: {},
    loading: false,
    error: null,
    lastRunAt: null
  }),

  getters: {
    currentManifest: (state) => (env: string) => state.manifests[env] ?? null,
    currentTopHub: (state) => (env: string) => state.topHubInsights[env] ?? null
  },

  actions: {
    setManifest(env: string, manifest: DiscoveryManifest) {
      this.manifests[env] = manifest;
    },
    setTopHubInsight(env: string, insight: TopHubInsight) {
      this.topHubInsights[env] = insight;
    },
    setLoading(loading: boolean) {
      this.loading = loading;
    },
    setError(error: string | null) {
      this.error = error;
    },
    setLastRunAt(ts: string) {
      this.lastRunAt = ts;
    },
    clear() {
      this.manifests = {};
      this.topHubInsights = {};
      this.lastRunAt = null;
      this.error = null;
    }
  }
});
```

#### Composables (`/src/composables/useDiscovery.ts`)
```ts
import { ref } from 'vue';
import { useDiscoveryStore } from '@/stores/discovery';
import type { DiscoveryManifest, TopHubInsight } from '@/types/discovery';

export function useDiscovery() {
  const store = useDiscoveryStore();
  const runId = ref<string | null>(null);

  async function runDiscovery(env: string) {
    store.setLoading(true);
    store.setError(null);
    try {
      // Deterministic mocks for dev; replace with real CDN/API fetches in prod
      const [manifest, topHub] = await Promise.all([
        fetchDiscoveryManifest(env),
        fetchTopHubInsight(env)
      ]);

      store.setManifest(env, manifest);
      store.setTopHubInsight(env, topHub);
      store.setLastRunAt(new Date().toISOString());
      runId.value = `${env}-${Date.now()}`;
      return { manifest, topHub, runId: runId.value };
    } catch (err) {
      store.setError(err instanceof Error ? err.message : String(err));
      throw err;
    } finally {
      store.setLoading(false);
    }
  }

  return {
    runDiscovery,
    loading: store.loading,
    error: store.error,
    manifests: store.manifests,
    topHubInsights: store.topHubInsights,
    currentManifest: store.currentManifest,
    currentTopHub: store.currentTopHub,
    lastRunAt: store.lastRunAt,
    runId
  };
}

// Deterministic fixtures (replace with real fetches in prod)
async function fetchDiscoveryManifest(env: string): Promise<DiscoveryManifest> {
  await new Promise((r) => setTimeout(r, 200));
  return {
    env,
    generatedAt: new Date().toISOString(),
    version: '4.2.0',
    summary: {
      totalResources: 214,
      totalMonthlyCost: 12840.5,
      servicesCount: 18,
      regionsCount: 7
    },
    resources: [
      {
        id: `i-${env}-001`,
        name: `web-tier-${env}-a`,
        type: 'ec2',
        region: 'us-east-1',
        accountId: '123456789012',
        tags: { Env: env, Owner: 'platform' },
        cost: { monthly: 120.0, currency: 'USD' },
        metadata: { instanceType: 'm5.large', az: 'us-east-1a' }
      }
    ],
    signals: [
      {
        id: 'sig-001',
        type: 'inefficiency',
        severity: 'medium',
        title: 'Underutilized instances',
        description: '3 instances <15% avg CPU over 14 days',
        context: { threshold: 0.15, periodDays: 14 },
        recommendation: 'Consider rightsizing to smaller instance types or enable scheduling stop/start.',
        resourceIds: ['i-prod-001', 'i-prod-002', 'i-prod-003']
      }
    ]
  };
}

async function fetchTopHubInsight(env: string): Promise<TopHubInsight> {
  await new Promise((r) =>
