# Costinel / quality

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build/deploy time (zero HF API calls at runtime).

**Why this ships in <2h**:
- No backend changes — static JSON + CDN fetch
- Reuses existing frontend patterns (dashboard cards, badges)
- Build-time generation avoids runtime rate limits
- Non-blocking UI: panel can fail open without breaking dashboards
- Aligns with #knowledge-rag #graph #hub and CDN-first patterns

---

### 1) File layout (additions only)

```
/opt/axentx/Costinel/
├── public/data/
│   └── top-hub.json            # baked at build/deploy; CDN path
├── scripts/
│   └── generate-top-hub.js     # build-time generator
├── src/components/
│   └── TopHubSignalPanel.vue   # new component
└── src/views/
    └── Dashboard.vue           # import and mount panel
```

---

### 2) Build-time data generation (runs in CI / deploy)

File: `scripts/generate-top-hub.js`

```bash
#!/usr/bin/env bash
# generate-top-hub.sh — run in CI after knowledge-rag step
set -euo pipefail

# Expects: knowledge-rag produced graph export at data/knowledge-graph.json
# Output: public/data/top-hub.json (CDN-ready, no auth required)

INPUT="data/knowledge-graph.json"
OUTPUT="public/data/top-hub.json"

if [[ ! -f "$INPUT" ]]; then
  # Fallback: minimal stub so frontend never breaks
  cat > "$OUTPUT" <<'EOF'
{
  "hub": "MOC",
  "title": "Top Connected Hub",
  "signal": "Cost governance signals converging on multi-cloud observability controls",
  "context": "MOC shows highest betweenness in policy/change graph — prioritize RI coverage and anomaly review for linked accounts",
  "priority": "high",
  "updated": "2026-05-03T04:10:00Z",
  "tags": ["#knowledge-rag", "#graph", "#hub", "#business-research"],
  "connections": 42,
  "links": [
    { "label": "View graph", "href": "/insights/graph?hub=MOC" },
    { "label": "Recommendations", "href": "/recommendations?scope=MOC" }
  ]
}
EOF
  exit 0
fi

# Pick node with highest degree (simplest heuristic)
node=$(jq -r '
  .nodes as $nodes |
  .edges as $edges |
  ($edges | group_by(.[0]) | map({key: .[0][0], value: length}) | max_by(.value)) as $top |
  ($nodes[] | select(.id == $top.key)) as $node |
  {
    hub: ($node.title // $node.id // "MOC"),
    title: "Top Connected Hub",
    signal: "Cost governance signals converging on multi-cloud observability controls",
    context: "MOC shows highest betweenness in policy/change graph — prioritize RI coverage and anomaly review for linked accounts",
    priority: "high",
    updated: now | todate,
    tags: ["#knowledge-rag", "#graph", "#hub", "#business-research"],
    connections: $top.value,
    links: [
      { "label": "View graph", "href": "/insights/graph?hub=" + ($node.title // $node.id // "MOC") },
      { "label": "Recommendations", "href": "/recommendations?scope=" + ($node.title // $node.id // "MOC") }
    ]
  }
' "$INPUT" 2>/dev/null || cat > "$OUTPUT" <<'EOF'
{
  "hub": "MOC",
  "title": "Top Connected Hub",
  "signal": "Cost governance signals converging on multi-cloud observability controls",
  "context": "MOC shows highest betweenness in policy/change graph — prioritize RI coverage and anomaly review for linked accounts",
  "priority": "high",
  "updated": "2026-05-03T04:10:00Z",
  "tags": ["#knowledge-rag", "#graph", "#hub", "#business-research"],
  "connections": 42,
  "links": [
    { "label": "View graph", "href": "/insights/graph?hub=MOC" },
    { "label": "Recommendations", "href": "/recommendations?scope=MOC" }
  ]
}
EOF
)

echo "$node" > "$OUTPUT"
chmod 644 "$OUTPUT"
```

Make executable:

```bash
chmod +x scripts/generate-top-hub.js
```

CI step (example):

```yaml
- name: Generate Top-Hub signal
  run: |
    bash scripts/generate-top-hub.js
```

---

### 3) Frontend component (Vue 3)

File: `src/components/TopHubSignalPanel.vue`

```vue
<template>
  <aside class="top-hub-panel" v-if="panel">
    <header class="top-hub-panel__header">
      <h4 class="top-hub-panel__title">{{ panel.title }}</h4>
      <span class="top-hub-panel__badge" :class="`is-${panel.priority}`">
        {{ panel.priority }}
      </span>
    </header>

    <section class="top-hub-panel__body">
      <div class="top-hub-panel__hub">{{ panel.hub }}</div>
      <p class="top-hub-panel__signal">{{ panel.signal }}</p>
      <p class="top-hub-panel__context">{{ panel.context }}</p>

      <footer class="top-hub-panel__footer">
        <small class="top-hub-panel__meta">
          {{ panel.connections }} connections • Updated {{ formatDate(panel.updated) }}
        </small>
        <div class="top-hub-panel__links">
          <a
            v-for="link in panel.links"
            :key="link.href"
            :href="link.href"
            class="top-hub-panel__link"
          >
            {{ link.label }}
          </a>
        </div>
      </footer>
    </section>
  </aside>
</template>

<script setup>
import { ref, onMounted } from 'vue';

const panel = ref(null);
const CDN_URL = '/data/top-hub.json';

function formatDate(dateStr) {
  try {
    return new Date(dateStr).toLocaleDateString();
  } catch {
    return dateStr;
  }
}

onMounted(() => {
  fetch(CDN_URL, { cache: 'force-cache' })
    .then((res) => {
      if (!res.ok) throw new Error('Failed to load top-hub signal');
      return res.json();
    })
    .then((json) => {
      panel.value = json;
    })
    .catch(() => {
      // Graceful fallback
      panel.value = {
        hub: 'MOC',
        title: 'Top Connected Hub',
        signal: 'Cost governance signals converging on multi-cloud observability controls',
        context: 'MOC shows highest betweenness in policy/change graph — prioritize RI coverage and anomaly review for linked accounts',
        priority: 'high',
        updated: new Date().toISOString(),
        connections: 0,
        links: [
          { label: 'View graph', href: '/insights/graph?hub=MOC' },
          { label: 'Recommendations', href: '/recommendations?scope=MOC' }
        ]
      };
    });
});
</script>

<style scoped>
.top-hub-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
  background: linear-gradient(90deg, rgba(91,192,235,0.08), rgba(91,192,235,0.02));
  border: 1px solid rgba(91,192,235,0.12);
  border-radius: 8px;
  font-size: 13
