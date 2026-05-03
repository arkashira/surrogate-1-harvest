# Costinel / frontend

## Final Synthesis — Costinel Top-Hub Signal Panel (Correct + Actionable)

**Goal:** One read-only panel that surfaces the top-hub signal and actionable proposals from `/api/v1/sense/top-hub-signal` without any execution capability.  
**Target effort:** ≤2h.  
**Non-negotiable:** Sense + Signal only — no execution; audit trail via acknowledge-only action.

---

### 1) Design decisions (resolved)

- **Endpoint:** `GET /api/v1/sense/top-hub-signal` (read-only)  
- **Auth:** Reuse existing bearer token (from app/auth store). Inject `Authorization: Bearer <token>`.  
- **Polling:** 60s refresh (configurable) with cancel-on-unmount; manual refresh button always available.  
- **Error/retry:** Exponential backoff (max 3) and respect `Retry-After` on 429/503.  
- **UI placement:** Dashboard sidebar/top-level widget labeled **Top-Hub Signal**.  
- **UX content:** Hub name, score, short rationale, 1–3 proposals; each proposal has **Acknowledge** (audit only).  
- **No execution:** All actions are signals; backend owns execution/audit.

---

### 2) File changes (minimal, concrete)

- `src/types/sense.ts` — shared types  
- `src/api/sense.ts` — typed API client  
- `src/hooks/useTopHubSignal.ts` — fetch + polling + backoff  
- `src/stores/sense.ts` — optional Pinia store (keeps compatibility with existing Vue code)  
- `src/components/TopHubSignalPanel/TopHubSignalPanel.vue` — presentational component (Vue)  
- `src/views/Dashboard.vue` — mount panel  
- `src/locales/en.json` — i18n keys

(If the project uses React, swap the `.vue` component for a `.tsx` equivalent using the same hook.)

---

### 3) Implementation (combined strongest parts)

#### `src/types/sense.ts`
```ts
export interface Proposal {
  id: string;
  title: string;
  description: string;
  impact: 'High' | 'Medium' | 'Low' | string;
}

export interface TopHubSignal {
  hub: string;
  score: number;
  rationale: string;
  proposals: Proposal[];
  updatedAt?: string;
}

export interface AcknowledgeResponse {
  acknowledged: boolean;
  proposalId: string;
  timestamp: string;
}
```

#### `src/api/sense.ts`
```ts
import axios from '@/plugins/axios';

export const senseApi = {
  async getTopHubSignal() {
    const res = await axios.get<TopHubSignal>('/api/v1/sense/top-hub-signal');
    return res.data;
  },

  async acknowledgeProposal(proposalId: string) {
    const res = await axios.post<AcknowledgeResponse>(
      `/api/v1/sense/proposals/${proposalId}/acknowledge`,
      {}
    );
    return res.data;
  }
};
```

#### `src/hooks/useTopHubSignal.ts`
```ts
import { ref, onMounted, onUnmounted } from 'vue';
import { senseApi } from '@/api/sense';
import type { TopHubSignal } from '@/types/sense';

export function useTopHubSignal(pollIntervalMs = 60_000) {
  const data = ref<TopHubSignal | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  const fetchWithBackoff = async (attempt = 0): Promise<TopHubSignal | null> => {
    const maxAttempts = 3;
    try {
      const result = await senseApi.getTopHubSignal();
      error.value = null;
      return result;
    } catch (err: any) {
      const status = err?.response?.status;
      const retryAfter = Number(err?.response?.headers?.['retry-after'] || 0) * 1000 || 1000 * Math.pow(2, attempt);

      if (status === 429 && attempt < maxAttempts) {
        await new Promise((r) => setTimeout(r, retryAfter));
        return fetchWithBackoff(attempt + 1);
      }

      if (attempt < maxAttempts) {
        await new Promise((r) => setTimeout(r, retryAfter));
        return fetchWithBackoff(attempt + 1);
      }

      error.value = err?.message || 'Failed to load top-hub signal';
      return null;
    }
  };

  const load = async () => {
    loading.value = true;
    const result = await fetchWithBackoff();
    data.value = result;
    loading.value = false;
  };

  const startPolling = () => {
    stopPolling();
    pollTimer = setInterval(load, pollIntervalMs);
  };

  const stopPolling = () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  };

  const refresh = () => {
    stopPolling();
    return load().finally(startPolling);
  };

  onMounted(() => {
    void load();
    startPolling();
  });

  onUnmounted(() => {
    stopPolling();
  });

  return { data, loading, error, refresh };
}
```

#### `src/stores/sense.ts` (Pinia — optional, for shared state)
```ts
import { defineStore } from 'pinia';
import { senseApi } from '@/api/sense';
import type { TopHubSignal } from '@/types/sense';

export const useSenseStore = defineStore('sense', {
  state: () => ({
    topHub: null as TopHubSignal | null,
    loading: false,
    error: null as string | null,
  }),

  actions: {
    async fetchTopHubSignal() {
      this.loading = true;
      this.error = null;
      try {
        this.topHub = await senseApi.getTopHubSignal();
      } catch (err: any) {
        this.error = err.message || 'Failed to load top-hub signal';
      } finally {
        this.loading = false;
      }
    },

    async acknowledgeProposal(proposalId: string) {
      try {
        await senseApi.acknowledgeProposal(proposalId);
        // refresh to reflect audit update
        await this.fetchTopHubSignal();
      } catch {
        // non-blocking: keep UI usable
      }
    },
  },
});
```

#### `src/components/TopHubSignalPanel/TopHubSignalPanel.vue`
```vue
<template>
  <div class="top-hub-signal-card">
    <div class="card-header">
      <h3>{{ $t('sense.topHubSignal') }}</h3>
      <button @click="refresh" :disabled="loading" class="btn-refresh" :title="$t('common.refresh')">
        ↻
      </button>
    </div>

    <div v-if="loading && !data" class="loading">{{ $t('common.loading') }}</div>

    <div v-else-if="error" class="error">
      {{ $t('common.error') }}: {{ error }}
    </div>

    <div v-else-if="!data" class="empty">
      {{ $t('sense.noSignal') }}
    </div>

    <div v-else class="signal-content">
      <div class="hub-header">
        <span class="hub-name">{{ data.hub }}</span>
        <span class="hub-score">{{ data.score }}</span>
      </div>
      <p class="hub-rationale">{{ data.rationale }}</p>

      <div class="proposals">
        <div v-for="p in data.proposals" :key="p.id" class="proposal">
          <div class="proposal-title">{{ p.title }}</div>
          <div class="proposal-desc">{{ p.description }}</div>
          <div class="proposal-meta">
            <span class="impact">{{ p.impact }}</span>
            <button @click="ack(p.id)" class="btn-ack">
              {{ $t('sense.acknow
