# Costinel / frontend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard (sidebar or top banner area).
- Defaults to hub **MOC** (configurable at build-time via `VITE_HUB_NAME`).
- CDN-only data fetch (bypasses HF API rate limits) using a single pre-listed file manifest.
- Lightweight, SSR-safe React component with skeleton states and graceful fallback.
- No backend changes; pure frontend addition.

### Why this is highest-value (<2h)
- Reuses known pattern (#top-hub doc insight, #knowledge-rag) to surface contextual insights immediately.
- CDN-bypass pattern avoids HF 429s during dev/demo.
- Small surface area: one component + one util + one config + minor route mount.

---

### File changes

#### 1) Config (build-time hub selection)
`src/config/hub.ts`
```ts
export const HUB = {
  NAME: import.meta.env.VITE_HUB_NAME || 'MOC',
  CDN_BASE: 'https://huggingface.co/datasets',
  REPO: import.meta.env.VITE_HUB_REPO || 'axentx/top-hub-insights',
  MANIFEST_PATH: import.meta.env.VITE_HUB_MANIFEST || 'latest/manifest.json',
};
```

#### 2) CDN manifest loader (zero-auth, rate-limit safe)
`src/lib/hub-cdn.ts`
```ts
import { HUB } from '../config/hub';

export interface HubManifest {
  files: Array<{
    path: string;
    size: number;
    sha256?: string;
    updated_at?: string;
  }>;
  generated_at?: string;
}

export interface HubSignal {
  title: string;
  summary: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  href?: string;
  tags?: string[];
}

const resolveCDNUrl = (repo: string, path: string) =>
  `${HUB.CDN_BASE}/${repo}/resolve/main/${path}`;

export async function fetchHubManifest(): Promise<HubManifest | null> {
  try {
    const res = await fetch(resolveCDNUrl(HUB.REPO, HUB.MANIFEST_PATH), {
      cache: 'no-store',
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function fetchLatestSignal(): Promise<HubSignal | null> {
  try {
    const manifest = await fetchHubManifest();
    if (!manifest?.files?.length) return null;

    // Prefer latest JSON by path convention: signals/{hub}/YYYY-MM-DD.json
    const hubFile = manifest.files
      .filter((f) => f.path.includes(`signals/${HUB.NAME.toLowerCase()}`) && f.path.endsWith('.json'))
      .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))[0];

    if (!hubFile) return null;

    const res = await fetch(resolveCDNUrl(HUB.REPO, hubFile.path), {
      cache: 'no-store',
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}
```

#### 3) Top-Hub Signal Panel component
`src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from 'react';
import { fetchLatestSignal } from '../lib/hub-cdn';
import { HUB } from '../config/hub';

const severityColors = {
  low: 'bg-blue-50 border-blue-200 text-blue-800',
  medium: 'bg-yellow-50 border-yellow-200 text-yellow-800',
  high: 'bg-orange-50 border-orange-200 text-orange-800',
  critical: 'bg-red-50 border-red-200 text-red-800',
};

export default function TopHubSignalPanel() {
  const [signal, setSignal] = useState<null | import('../lib/hub-cdn').HubSignal>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetchLatestSignal().then((s) => {
      if (mounted) {
        setSignal(s);
        setLoading(false);
      }
    });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="flex items-center gap-3 rounded-lg border p-3">
        <div className="h-10 w-10 shrink-0 rounded bg-gray-100 animate-pulse" />
        <div className="flex-1 space-y-2">
          <div className="h-4 w-3/4 rounded bg-gray-100 animate-pulse" />
          <div className="h-3 w-5/6 rounded bg-gray-100 animate-pulse" />
        </div>
      </div>
    );
  }

  if (!signal) return null;

  return (
    <a
      href={signal.href}
      target={signal.href ? '_blank' : undefined}
      rel={signal.href ? 'noopener noreferrer' : undefined}
      className={`block rounded-lg border p-3 transition-colors ${severityColors[signal.severity]}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="shrink-0 rounded-full bg-black/5 px-2 py-1 text-xs font-semibold uppercase tracking-wide">
            {HUB.NAME}
          </div>
          <div>
            <p className="font-semibold text-sm">{signal.title}</p>
            <p className="text-xs opacity-80">{signal.summary}</p>
            {signal.tags?.length ? (
              <div className="mt-2 flex flex-wrap gap-1">
                {signal.tags.map((t) => (
                  <span
                    key={t}
                    className="rounded bg-black/5 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide"
                  >
                    {t}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <span className="text-[10px] font-bold uppercase tracking-wider">{signal.severity}</span>
        </div>
      </div>
    </a>
  );
}
```

#### 4) Mount on dashboard
Locate the main dashboard route/component (commonly `src/pages/Dashboard.tsx` or similar). Insert near the top of the main content area (below any page header, above metrics).

Example insertion:
```tsx
import TopHubSignalPanel from '../components/TopHubSignalPanel';

export default function Dashboard() {
  return (
    <div className="space-y-6">
      <TopHubSignalPanel />
      {/* rest of dashboard */}
    </div>
  );
}
```

#### 5) Optional: sidebar variant
If you prefer a sidebar card, create `src/components/SidebarHubSignal.tsx` with similar content and mount in the sidebar layout.

---

### Build & deploy steps (5 min)

1. Add env vars (optional) to `.env` or CI/CD:
   ```
   VITE_HUB_NAME=MOC
   VITE_HUB_REPO=axentx/top-hub-insights
   VITE_HUB_MANIFEST=latest/manifest.json
   ```

2. Ensure manifest exists at CDN path (one-time infra task, can be done by ops):
   - `latest/manifest.json` should list available signal files.
   - Signal file example: `signals/moc/2026-05-03.json`
   - Signal file schema:
     ```json
     {
       "title": "MOC: Reserved Instance underutilization",
       "summary": "3 x m5.xlarge in us-east-1 show <20% CPU over 14 days. Estimated savings
