# Costinel / discovery

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted in the Costinel dashboard (sidebar or top banner).
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows:
  - Hub name + short insight (1–2 sentences)
  - Top 3 related docs (title + snippet)
  - Last updated timestamp
- **CDN-first**: loads from `public/hubs/{hubName}.json` (no auth, no API, no rate limit).
- **Graceful fallback**: if file missing or malformed, panel collapses silently (no errors in UI).
- **Zero backend changes** — pure frontend + static asset.

### Why this is highest-value (<2h)
- Reuses the known pattern: “Review the most-connected hub (e.g., MOC) before planning tasks”.
- Avoids infra, auth, and rate-limit concerns by using CDN static assets.
- Delivers immediate contextual value to Costinel users without touching billing/compute.
- Fits the “Sense + Signal — ไม่ Execute” philosophy (shows insight, doesn’t act).

---

### File changes

#### 1) Add static hub data (example for MOC)
`public/hubs/MOC.json`
```json
{
  "hub": "MOC",
  "insight": "MOC remains the most-connected hub for cost governance signals — prioritize RI coverage and anomaly triage in linked accounts.",
  "relatedDocs": [
    {
      "title": "Reserved Instance Coverage Analysis",
      "snippet": "How to calculate RI coverage gaps across AWS accounts and regions."
    },
    {
      "title": "Anomaly Detection Playbook",
      "snippet": "Step-by-step runbook for investigating cost spikes and tag drift."
    },
    {
      "title": "Multi-Account Tag Governance",
      "snippet": "Enforce tag policies and allocation tags without blocking deployments."
    }
  ],
  "updatedAt": "2026-05-03T04:00:00Z"
}
```

#### 2) Add lightweight panel component
`src/components/TopHubSignalPanel.vue`
```vue
<template>
  <aside v-if="hasContent" class="top-hub-panel" aria-label="Top hub signal">
    <header class="top-hub-panel__header">
      <strong class="top-hub-panel__hub">{{ hub }}</strong>
      <time class="top-hub-panel__time" :datetime="updatedAt">{{ formattedTime }}</time>
    </header>
    <p class="top-hub-panel__insight">{{ insight }}</p>
    <ul class="top-hub-panel__docs" aria-label="Related docs">
      <li v-for="(doc, i) in relatedDocs" :key="i" class="top-hub-panel__doc">
        <strong class="top-hub-panel__doc-title">{{ doc.title }}</strong>
        <span class="top-hub-panel__doc-snippet">{{ doc.snippet }}</span>
      </li>
    </ul>
  </aside>
</template>

<script>
export default {
  name: "TopHubSignalPanel",
  props: {
    hubName: { type: String, default: () => import.meta.env.VITE_HUB_NAME || "MOC" }
  },
  data() {
    return {
      hub: "",
      insight: "",
      relatedDocs: [],
      updatedAt: "",
      hasContent: false
    };
  },
  computed: {
    formattedTime() {
      if (!this.updatedAt) return "";
      return new Date(this.updatedAt).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric"
      });
    }
  },
  async mounted() {
    await this.loadHub();
  },
  methods: {
    async loadHub() {
      try {
        // CDN-first: public/ is served at root, no auth required
        const res = await fetch(`/hubs/${this.hubName}.json`, { cache: "no-store" });
        if (!res.ok) return; // graceful silent fallback
        const json = await res.json();

        // Minimal validation
        if (!json || typeof json !== "object") return;
        if (!json.hub || !Array.isArray(json.relatedDocs)) return;

        this.hub = json.hub;
        this.insight = json.insight || "";
        this.relatedDocs = json.relatedDocs.slice(0, 3);
        this.updatedAt = json.updatedAt || "";
        this.hasContent = true;
      } catch {
        // Silent fail — do not block UI or log noisy errors
        this.hasContent = false;
      }
    }
  }
};
</script>

<style scoped>
.top-hub-panel {
  padding: 12px 16px;
  margin: 0 0 16px 0;
  border-radius: 8px;
  background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
  border: 1px solid #e2e8f0;
  color: #0f172a;
  font-size: 13px;
  line-height: 1.4;
}
.top-hub-panel__header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.top-hub-panel__hub {
  font-size: 14px;
  color: #0ea5e9;
}
.top-hub-panel__time {
  font-size: 11px;
  color: #64748b;
  white-space: nowrap;
}
.top-hub-panel__insight {
  margin: 0 0 8px 0;
  font-size: 13px;
  color: #334155;
}
.top-hub-panel__docs {
  list-style: none;
  padding: 0;
  margin: 0;
}
.top-hub-panel__doc {
  padding: 4px 0;
  border-bottom: 1px dashed #e2e8f0;
}
.top-hub-panel__doc:last-child {
  border-bottom: none;
}
.top-hub-panel__doc-title {
  display: block;
  font-size: 12px;
  color: #0f172a;
}
.top-hub-panel__doc-snippet {
  font-size: 12px;
  color: #64748b;
}
</style>
```

#### 3) Mount panel in dashboard layout
`src/views/Dashboard.vue` (or wherever the main dashboard lives)
```vue
<template>
  <div class="dashboard">
    <!-- Top-Hub Signal Panel (non-blocking) -->
    <TopHubSignalPanel :hub-name="hubName" />

    <!-- existing dashboard content below -->
    <section class="dashboard__content">
      <!-- ... existing dashboard widgets ... -->
    </section>
  </div>
</template>

<script>
import TopHubSignalPanel from "@/components/TopHubSignalPanel.vue";

export default {
  name: "Dashboard",
  components: { TopHubSignalPanel },
  data() {
    return {
      hubName: import.meta.env.VITE_HUB_NAME || "MOC"
    };
  }
};
</script>
```

#### 4) Optional env var for hub selection
`.env` (or CI/CD)
```
VITE_HUB_NAME=MOC
```

---

### Validation checklist (quick)
- [ ] `public/hubs/MOC.json` exists and is valid JSON.
- [ ] Component mounts without console errors.
- [ ] Panel is visible when JSON present; invisible when missing or invalid.
- [ ] No network auth headers sent (verify in DevTools).
- [ ] Build produces no errors (`npm run build`).

---

### Deployment notes
- Commit `public/hubs/MOC.json` and `TopHubSignalPanel.vue`.
- No backend or infra changes required.
- CDN serves file immediately; cache headers default to short/no-store for freshness.
- To update insights: edit the JSON and redeploy frontend (or update via CI).
