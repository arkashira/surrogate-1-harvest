# Costinel / backend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on `/dashboard`.
- Defaults to hub **MOC** (configurable via `HUB_NAME`).
- Shows: hub title, short description, top 5 insights, and last-updated timestamp.
- **CDN-first data strategy**: pre-listed file paths embedded in the frontend build; runtime fetches use `https://huggingface.co/datasets/.../resolve/main/...` (no API/auth, bypasses rate limits).
- Telemetry-aware: emits `hub_panel_impression` and `hub_panel_fetch` events (non-blocking, best-effort).

### Why this is highest-value (<2h)
- Reuses existing `#knowledge-rag #graph #hub` patterns (MOC is the most-connected hub).
- No backend changes required — pure frontend addition with CDN static assets.
- Fits Costinel philosophy: **Sense + Signal** — shows insights, does not execute.
- Low risk, fast feedback loop for dashboard engagement.

---

### File changes (concrete)

#### 1) Add panel component: `src/components/TopHubSignalPanel.tsx`
```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState } from "react";
import { ExternalLink } from "lucide-react";

type Insight = {
  title: string;
  summary: string;
  href?: string;
};

type HubManifest = {
  title: string;
  description: string;
  updated: string; // ISO
  insights: Insight[];
};

const HUB_NAME = import.meta.env.VITE_HUB_NAME || "MOC";
// CDN base — public dataset files bypass HF API rate limits
const CDN_BASE = `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/${HUB_NAME}`;

export default function TopHubSignalPanel() {
  const [manifest, setManifest] = useState<HubManifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Telemetry: impression
    try {
      navigator.sendBeacon?.(
        "/telemetry",
        JSON.stringify({ event: "hub_panel_impression", hub: HUB_NAME, ts: Date.now() })
      );
    } catch (e) {
      // non-blocking
    }

    fetch(`${CDN_BASE}/manifest.json`, { cache: "no-cache" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        setManifest(data);
        setLoading(false);
        // Telemetry: successful fetch
        try {
          navigator.sendBeacon?.(
            "/telemetry",
            JSON.stringify({ event: "hub_panel_fetch", hub: HUB_NAME, ok: true, ts: Date.now() })
          );
        } catch (e) {}
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
        try {
          navigator.sendBeacon?.(
            "/telemetry",
            JSON.stringify({ event: "hub_panel_fetch", hub: HUB_NAME, ok: false, error: err.message, ts: Date.now() })
          );
        } catch (e) {}
      });
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card/50 p-4 animate-pulse">
        <div className="h-5 w-32 bg-muted rounded mb-2"></div>
        <div className="h-4 w-full bg-muted rounded mb-1"></div>
        <div className="h-4 w-5/6 bg-muted rounded mb-3"></div>
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-4 bg-muted rounded"></div>
          ))}
        </div>
      </div>
    );
  }

  if (error || !manifest) {
    return (
      <div className="rounded-lg border bg-card/50 p-4 text-sm text-muted-foreground">
        Unable to load hub signals.
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card/50 p-4">
      <div className="flex items-start justify-between mb-2">
        <div>
          <h3 className="font-semibold text-sm">{manifest.title}</h3>
          <p className="text-xs text-muted-foreground">{manifest.description}</p>
        </div>
        <span className="text-[10px] text-muted-foreground/60 uppercase tracking-wider">
          {new Date(manifest.updated).toLocaleDateString()}
        </span>
      </div>

      <ul className="mt-3 space-y-2">
        {manifest.insights.slice(0, 5).map((item, idx) => (
          <li key={idx} className="text-sm">
            <div className="font-medium text-foreground">{item.title}</div>
            <div className="text-xs text-muted-foreground">{item.summary}</div>
            {item.href && (
              <a
                href={item.href}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-primary flex items-center gap-1 mt-1 hover:underline"
              >
                View details <ExternalLink className="w-3 h-3" />
              </a>
            )}
          </li>
        ))}
      </ul>

      <div className="mt-3 pt-3 border-t border-border/50 text-right">
        <a
          href={`https://huggingface.co/datasets/axentx/knowledge-rag/tree/main/hubs/${HUB_NAME}`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-muted-foreground hover:text-foreground flex items-center justify-end gap-1"
        >
          View hub on HF <ExternalLink className="w-3 h-3" />
        </a>
      </div>
    </div>
  );
}
```

#### 2) Add CDN manifest example (seed file) for MOC
Create: `public/hubs/MOC/manifest.json` (or ensure HF dataset path exists). For immediate local dev, seed a minimal file in repo so panel works without HF fetch during initial testing.

`src/data/moc-manifest.json` (seed; build can copy to CDN path later):
```json
{
  "title": "MOC — Multi-Org Cost Signals",
  "description": "Top cross-org cost anomalies and optimization signals curated from knowledge-rag graph.",
  "updated": "2026-05-03T03:00:00Z",
  "insights": [
    {
      "title": "Idle dev clusters detected (3 accounts)",
      "summary": "Non-production clusters running >18h/day; estimated $4.2k/mo savings with scheduled stop/start.",
      "href": "https://costinel.example.com/proposals/123"
    },
    {
      "title": "Underutilized RIs in us-east-1",
      "summary": "RI coverage 38% for m5.large family; rightsizing or convertible RIs could reduce cost 22%.",
      "href": "https://costinel.example.com/proposals/124"
    },
    {
      "title": "Orphaned volumes (23 total)",
      "summary": "Unattached gp3 volumes totaling 1.2TB; deletion candidates after 7-day retention.",
      "href": "https://costinel.example.com/proposals/125"
    },
    {
      "title": "SavingsPlan coverage gap",
      "summary": "Compute SavingsPlan covers 54% of eligible spend; increasing to 75% could lock additional 8% discount.",
      "href": "https://costinel.example.com/proposals/126"
    },
    {
      "title": "Cross-account tag compliance drift",
      "summary": "17% of resources missing cost-center tag; automated tagging policy recommended.",
      "href": "https
