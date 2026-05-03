# Costinel / quality

**Final Implementation Plan — Top-Hub Signal Card (Costinel)**

---

### 1. Architecture Decision (Unified)
- **Location**: `src/components/cards/TopHubSignalCard.vue`  
  (Vue, not React — matches existing Costinel stack; avoids dual-framework cost/risk.)
- **Data Flow**:  
  - Primary: lightweight `useTopHubInsights` composable that queries knowledge-RAG (or local cache).  
  - Fallback: static JSON in `public/mock-data/top-hub-moc.json` for offline/dev.
- **Pattern**: Read-only “Sense + Signal” card. No execution capability.  
- **Composition**: Uses existing `BaseCard`, `Badge`, `Tag` components for styling consistency.

---

### 2. Type Definitions (Shared)

**File**: `src/types/knowledge-rag.d.ts`
```ts
export interface HubNode {
  id: string;
  label: string;
  category?: string;
  type: 'hub' | 'document' | 'concept';
  connections: number;
  lastUpdated: string;
}

export interface RAGInsight {
  id: string;
  hubId: string;
  title: string;
  summary: string;
  source: string;
  relevance: number;
  tags: string[];
}

export interface TopHubData {
  hub: string;
  category: string;
  title: string;
  summary: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
  connections: number;
  updatedAt: string;
  signals: Array<{ label: string; value: string }>;
  tags: string[];
}
```

---

### 3. Lightweight Data Layer

**File**: `src/utils/knowledgeRagClient.ts`
```ts
import type { TopHubData } from '@/types/knowledge-rag';

export async function fetchTopHubInsights(): Promise<TopHubData | null> {
  try {
    // Replace with real RAG endpoint when available
    const res = await fetch('/api/knowledge-rag/top-hub');
    if (!res.ok) throw new Error('RAG endpoint unavailable');
    return await res.json();
  } catch {
    // Fallback to static mock
    const mod = await import('/mock-data/top-hub-moc.json');
    return mod.default as TopHubData;
  }
}
```

**File**: `src/hooks/useTopHubInsights.ts`
```ts
import { ref } from 'vue';
import { fetchTopHubInsights } from '@/utils/knowledgeRagClient';
import type { TopHubData } from '@/types/knowledge-rag';

export function useTopHubInsights() {
  const data = ref<TopHubData | null>(null);
  const loading = ref(false);
  const error = ref<Error | null>(null);

  async function load() {
    loading.value = true;
    error.value = null;
    try {
      data.value = await fetchTopHubInsights();
    } catch (e) {
      error.value = e instanceof Error ? e : new Error('Failed to load top hub');
    } finally {
      loading.value = false;
    }
  }

  return { data, loading, error, load };
}
```

---

### 4. Component Implementation

**File**: `src/components/cards/TopHubSignalCard.vue`
```vue
<template>
  <BaseCard
    :title="cardTitle"
    :icon="BrainIcon"
    :variant="insightSeverity"
    class="top-hub-signal-card"
  >
    <!-- Header: Identity -->
    <template #header>
      <div class="hub-identity">
        <Badge :variant="insightSeverity" class="hub-badge">
          {{ hubData.hub || 'MOC' }}
        </Badge>
        <span class="hub-tag">{{ hubData.category || 'Knowledge Hub' }}</span>
      </div>
    </template>

    <!-- Core Insight -->
    <div class="insight-content">
      <h3 class="insight-title">{{ hubData.title }}</h3>
      <p class="insight-summary">{{ hubData.summary }}</p>
    </div>

    <!-- Contextual Signals -->
    <div class="context-grid">
      <div
        v-for="(signal, idx) in hubData.signals"
        :key="idx"
        class="context-signal"
      >
        <SignalIcon class="signal-icon" />
        <div class="signal-text">
          <strong>{{ signal.label }}:</strong>
          <span>{{ signal.value }}</span>
        </div>
      </div>
    </div>

    <!-- Tags -->
    <div class="tag-cloud">
      <Tag
        v-for="tag in hubData.tags"
        :key="tag"
        :label="tag"
        size="sm"
        variant="outline"
        class="hub-tag-item"
      />
    </div>

    <!-- Footer -->
    <template #footer>
      <div class="card-meta">
        <ClockIcon class="meta-icon" />
        <span class="meta-text">Updated {{ formatDate(hubData.updatedAt) }}</span>
        <div class="connection-strength">
          <span class="strength-label">Connections:</span>
          <Badge variant="info">{{ hubData.connections }}</Badge>
        </div>
      </div>
    </template>
  </BaseCard>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue';
import {
  Brain as BrainIcon,
  Clock as ClockIcon,
  Signal as SignalIcon,
} from '@heroicons/vue/24/outline';
import BaseCard from './BaseCard.vue';
import Badge from './Badge.vue';
import Tag from './Tag.vue';
import { useTopHubInsights } from '@/hooks/useTopHubInsights';
import type { TopHubData } from '@/types/knowledge-rag';

const { data, loading, load } = useTopHubInsights();

onMounted(() => {
  load();
});

const hubData = computed<TopHubData>(() =>
  data.value || {
    hub: 'MOC',
    category: 'Cloud Governance',
    title: 'Master of Cloud - Central Governance Node',
    summary:
      'Primary hub for multi-cloud cost optimization patterns, RI recommendations, and governance workflows. Connects 42 related decisions and 8 cost domains.',
    severity: 'high',
    connections: 42,
    updatedAt: '2026-04-27T14:30:00Z',
    signals: [
      { label: 'Active Decisions', value: '12' },
      { label: 'Cost Impact', value: '$2.4M/yr' },
      { label: 'Coverage', value: 'AWS/GCP/Azure' },
    ],
    tags: ['#knowledge-rag', '#graph', '#hub', '#MOC', '#governance'],
  }
);

const insightSeverity = computed(() => {
  const map = {
    critical: 'danger',
    high: 'warning',
    medium: 'info',
    low: 'muted',
  } as const;
  return map[hubData.value.severity] || 'info';
});

const cardTitle = computed(() => `Top Hub: ${hubData.value.hub}`);

function formatDate(dateString: string) {
  const d = new Date(dateString);
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
</script>

<style scoped>
.top-hub-signal-card {
  @apply border-l-4 border-l-blue-500;
}

.hub-identity {
  @apply flex items-center gap-2;
}

.hub-badge {
  @apply text-sm font-semibold;
}

.hub-tag {
  @apply text-xs text-gray-500 font-medium;
}

.insight-content {
  @apply mb-4;
}

.insight-title {
  @apply text-lg font-semibold text-gray-900 mb-2;
}

.insight-summary {
  @apply text-sm text-gray-60
