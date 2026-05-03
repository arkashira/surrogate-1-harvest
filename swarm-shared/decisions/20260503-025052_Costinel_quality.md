# Costinel / quality

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### Scope & Value
- Add a **non-blocking, CDN-first Top-Hub Signal Panel** to the Costinel dashboard.
- Defaults to hub **MOC** (configurable).
- Runtime fetches use CDN only (no HF API/auth during user sessions).
- Telemetry for observability (`hub_panel_impression`, `hub_panel_fetch`, `hub_panel_error`).
- Graceful degradation: skeleton → cached → collapsed with retry.

---

### Architecture Decisions (resolved)
- **Frontend**: Vue 3 Composition API (matches codebase).
- **Data flow**:
  - Build/CI step: one-time HF tree list → `src/config/hubs.json` (canonical source).
  - Runtime: load `hubs.json`, pick latest file for hub, fetch content via CDN (`resolve/main/...`).
  - Cache in `sessionStorage` (TTL 15m) to avoid repeat CDN hits per session.
- **Telemetry**: single lightweight helper using `sendBeacon` with `keepalive` fallback.
- **UI**: top-banner or right-sidebar slot; dismissible, keyboard-accessible, skeleton-first.

---

### Files to Add / Modify
1. `src/components/TopHubSignalPanel.vue` — panel UI.
2. `src/composables/useHubPanel.ts` — CDN fetch, cache, retry, selection logic.
3. `src/telemetry.ts` — telemetry helper.
4. `src/config/hubs.json` — generated file list + metadata.
5. `scripts/generate-hub-filelist.ts` — HF tree lister (run manually or in CI post rate-limit).
6. `.env` — add `VITE_HUB_NAME`, `VITE_HUB_REPO`, `VITE_HUB_PATH_PREFIX`, `VITE_HUB_CACHE_TTL`.
7. Mount in dashboard layout (`src/views/Dashboard.vue` or main layout).

---

### Step-by-step (≤2h)

1. **Add env variables** (5 min)
   ```bash
   VITE_HUB_NAME=MOC
   VITE_HUB_REPO=axentx/knowledge
   VITE_HUB_PATH_PREFIX=hub
   VITE_HUB_CACHE_TTL=900000
   ```

2. **Create telemetry helper** (`src/telemetry.ts`) (10 min)
   - Expose `track(event, payload)` using `sendBeacon` with `keepalive` fallback.

3. **Create composable** (`src/composables/useHubPanel.ts`) (30 min)
   - Load `hubs.json`.
   - Filter by `hubName` and `pathPrefix`.
   - Pick latest by `updatedAt`.
   - CDN fetch with exponential backoff (3 retries).
   - Cache in `sessionStorage` (TTL).
   - Emit telemetry events.

4. **Create component** (`src/components/TopHubSignalPanel.vue`) (30 min)
   - Skeleton loader.
   - Show hub title, short summary, “View insights” link.
   - Dismissible (local state).
   - Retry button on error.
   - Keyboard accessible.

5. **Generate file list** (`scripts/generate-hub-filelist.ts`) (20 min)
   - Use HF SDK on Mac orchestrator: single `list_repo_tree` call per folder.
   - Output `src/config/hubs.json` with `{ hub, path, updatedAt, size }`.

6. **Mount panel** in dashboard layout (10 min)

7. **Test** (15 min)
   - Run generate script (post rate-limit).
   - `npm run dev` — verify panel loads via CDN, no HF API calls at runtime.
   - Disable network — verify graceful collapse + retry.
   - Verify telemetry events in devtools.

---

### Code Snippets

#### 1) Environment (.env)
```bash
VITE_HUB_NAME=MOC
VITE_HUB_REPO=axentx/knowledge
VITE_HUB_PATH_PREFIX=hub
VITE_HUB_CACHE_TTL=900000
```

#### 2) Telemetry helper (src/telemetry.ts)
```ts
// src/telemetry.ts
export function track(event: string, payload: Record<string, unknown> = {}) {
  const data = {
    event,
    hub: import.meta.env.VITE_HUB_NAME || 'MOC',
    ts: Date.now(),
    ...payload,
  };

  try {
    if (navigator.sendBeacon) {
      const blob = new Blob([JSON.stringify(data)], { type: 'application/json' });
      navigator.sendBeacon('/_telemetry', blob);
    } else {
      fetch('/_telemetry', {
        method: 'POST',
        body: JSON.stringify(data),
        headers: { 'Content-Type': 'application/json' },
        keepalive: true,
      }).catch(() => {});
    }
  } catch {
    // noop
  }
}
```

#### 3) Composable (src/composables/useHubPanel.ts)
```ts
// src/composables/useHubPanel.ts
import { ref, computed, onMounted } from 'vue';

const CDN_ROOT = 'https://huggingface.co/datasets';

export interface HubFileMeta {
  hub: string;
  path: string;
  updatedAt: string;
  size: number;
}

export function useHubPanel() {
  const hubName = import.meta.env.VITE_HUB_NAME || 'MOC';
  const repo = import.meta.env.VITE_HUB_REPO || 'axentx/knowledge';
  const prefix = import.meta.env.VITE_HUB_PATH_PREFIX || 'hub';
  const ttl = Number(import.meta.env.VITE_HUB_CACHE_TTL) || 900000;

  const filelist = ref<HubFileMeta[]>([]);
  const current = ref<HubFileMeta & { content?: string } | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const dismissed = ref(false);

  async function loadFileList() {
    try {
      const res = await fetch('/config/hubs.json', { cache: 'no-store' });
      if (!res.ok) throw new Error('hubs.json unavailable');
      filelist.value = await res.json();
    } catch (e) {
      console.warn('[HubPanel] filelist load failed', e);
      filelist.value = [];
    }
  }

  async function fetchFromCDN(path: string) {
    const url = `${CDN_ROOT}/${repo}/resolve/main/${path}`;
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`CDN fetch ${res.status}`);
    return res.text();
  }

  async function load() {
    if (loading.value) return;
    loading.value = true;
    error.value = null;

    try {
      await loadFileList();

      const candidates = filelist.value.filter(
        (f) => f.hub === hubName && f.path.startsWith(prefix)
      );
      if (!candidates.length) {
        error.value = 'No hub files found';
        return;
      }

      candidates.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
      const pick = candidates[0];

      const cacheKey = `hub:${repo}:${pick.path}`;
      const cached = sessionStorage.getItem(cacheKey);
      if (cached) {
        const { ts, content } = JSON.parse(cached);
        if (Date.now() - ts < ttl) {
          current.value = { ...pick, content };
          loading.value = false;
          return;
        }
      }

      let attempts = 0;
      const maxAttempts = 3;
      let lastErr: Error | null = null;
      while (attempts < maxAttempts) {
        try {
          const content = await fetchFromCDN(pick.path);
          sessionStorage.setItem(cacheKey, JSON.stringify({ ts: Date.now(), content }));
          current.value = { ...pick, content };
          loading.value = false;
          track('hub_panel_fetch', { status: 'success', path: pick.path });
          return;
        } catch (e: any) {
          lastErr = e;
          attempts++;
          if (attempts < maxAttempts) await new Promise((r) => setTimeout(r, 800 * attempts));
        }
     
