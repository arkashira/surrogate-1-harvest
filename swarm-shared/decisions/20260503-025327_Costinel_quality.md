# Costinel / quality

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on `/dashboard`.
- Defaults to hub **MOC** (configurable via `HUB_NAME`).
- Shows: hub title, short description, top 3 actionable signals, last updated timestamp.
- CDN-first data strategy: single pre-listed JSON file served from `https://huggingface.co/datasets/axentx/costinel-hubs/resolve/main/{hub}/signals.json` (no API auth, no rate-limit).
- Fallback to local stub if CDN fails (keeps UI non-blocking).
- Zero backend changes; pure frontend addition + one build-time asset.

### Steps (≤2h)
1. Create hub data file (15m)
   - `mkdir -p public/hubs/MOC`
   - `public/hubs/MOC/signals.json` with 3 signals + metadata
2. Add env var (5m)
   - `.env` → `VITE_HUB_NAME=MOC`
3. Create component (30m)
   - `src/components/TopHubSignalPanel.svelte` (or `.tsx` depending on stack)
   - CDN fetch with timeout + fallback
   - Non-blocking: renders skeleton then fills; never throws to console
4. Mount on dashboard (15m)
   - Import and place in dashboard layout (top banner or right sidebar)
5. Styling & polish (25m)
   - Minimal styles matching existing design tokens
6. Test & build (10m)
   - Local dev verify; ensure no CORS issues; CDN URL reachable

---

### Code snippets

#### public/hubs/MOC/signals.json
```json
{
  "hub": "MOC",
  "title": "Mission Operations Center",
  "description": "Real-time ops signals and cost governance recommendations for production workloads.",
  "updatedAt": "2026-05-03T03:00:00Z",
  "signals": [
    {
      "id": "MOC-001",
      "severity": "high",
      "title": "Unattached EBS volumes detected",
      "description": "12 unattached volumes across us-east-1 and eu-west-1 (~$420/mo).",
      "action": "Review and schedule deletion",
      "link": "/dashboard/resources?filter=unattached-ebs"
    },
    {
      "id": "MOC-002",
      "severity": "medium",
      "title": "Low RI coverage on m5.large",
      "description": "RI coverage 42% for m5.large; estimated savings $1,850/mo at 80% target.",
      "action": "Run RI recommendation",
      "link": "/dashboard/recommendations/ri"
    },
    {
      "id": "MOC-003",
      "severity": "low",
      "title": "Idle dev clusters nights/weekends",
      "description": "3 non-prod clusters idle 65% of time; consider auto-stop policy.",
      "action": "Configure schedule",
      "link": "/dashboard/policies"
    }
  ]
}
```

#### .env
```
VITE_HUB_NAME=MOC
```

#### src/components/TopHubSignalPanel.svelte
```svelte
<script lang="ts">
  import { onMount } from "svelte";

  const hubName = import.meta.env.VITE_HUB_NAME || "MOC";
  const cdnUrl = `https://huggingface.co/datasets/axentx/costinel-hubs/resolve/main/${hubName}/signals.json`;
  const localFallbackUrl = `/hubs/${hubName}/signals.json`;

  type Signal = {
    id: string;
    severity: "high" | "medium" | "low";
    title: string;
    description: string;
    action: string;
    link: string;
  };

  type HubData = {
    hub: string;
    title: string;
    description: string;
    updatedAt: string;
    signals: Signal[];
  };

  let data: HubData | null = null;
  let loading = true;
  let error = false;

  async function fetchWithTimeout(url: string, timeout = 4000): Promise<Response> {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);
    try {
      const res = await fetch(url, { signal: controller.signal, cache: "no-store" });
      clearTimeout(id);
      return res;
    } catch {
      clearTimeout(id);
      throw new Error("fetch timeout or abort");
    }
  }

  async function load() {
    try {
      const res = await fetchWithTimeout(cdnUrl, 5000);
      if (!res.ok) throw new Error("CDN non-ok");
      data = await res.json();
    } catch (e) {
      // fallback to local
      try {
        const res = await fetch(localFallbackUrl, { cache: "no-store" });
        if (res.ok) data = await res.json();
        else error = true;
      } catch {
        error = true;
      }
    } finally {
      loading = false;
    }
  }

  onMount(load);

  $: severityClass = (s: Signal["severity"]) => ({ high: "severity-high", medium: "severity-medium", low: "severity-low" }[s] || "");
</script>

{#if loading}
  <div class="top-hub-panel skeleton" aria-busy="true">
    <div class="skeleton-line" style="width:60%"></div>
    <div class="skeleton-line" style="width:90%"></div>
    <div class="skeleton-line" style="width:70%"></div>
  </div>
{:else if data}
  <aside class="top-hub-panel" aria-label={`Top signals from ${data.title}`}>
    <header class="panel-header">
      <h3 class="panel-title">{data.title}</h3>
      <p class="panel-desc">{data.description}</p>
      <time class="panel-time" datetime={data.updatedAt}>{new Date(data.updatedAt).toLocaleString()}</time>
    </header>

    <ul class="signals-list" role="list">
      {#each data.signals as s}
        <li class="signal-item">
          <div class="signal-meta">
            <span class={`severity-badge ${severityClass(s.severity)}`}>{s.severity}</span>
            <span class="signal-id">{s.id}</span>
          </div>
          <h4 class="signal-title">{s.title}</h4>
          <p class="signal-desc">{s.description}</p>
          <a class="signal-action" href={s.link}>{s.action} →</a>
        </li>
      {/each}
    </ul>
  </aside>
{:else}
  <!-- Non-blocking: render nothing on failure -->
  <div class="top-hub-panel hidden" aria-hidden="true"></div>
{/if}

<style>
  .top-hub-panel {
    background: #fff;
    border: 1px solid #e6e9ee;
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 16px;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
  }

  .panel-header { margin-bottom: 12px; }
  .panel-title { margin: 0 0 4px; font-size: 16px; font-weight: 600; color: #111; }
  .panel-desc { margin: 0 0 6px; font-size: 13px; color: #556; }
  .panel-time { font-size: 12px; color: #889; }

  .signals-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 10px; }

  .signal-item {
    padding: 10px;
    border-radius
