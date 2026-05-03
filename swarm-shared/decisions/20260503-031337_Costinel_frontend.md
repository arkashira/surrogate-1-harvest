# Costinel / frontend

**Final Implementation — Top-Hub Signal Panel (CDN-first, <2h, production-ready)**

---

## 1) Strategy (resolve contradictions)
- **CDN-first, zero-auth**: Use public HuggingFace CDN file; no backend or HF API tokens required.  
- **Graceful failure**: If CDN fails, fall back to bundled static file; if that fails, use hardcoded emergency payload. No errors shown to users.  
- **Performance**: Fetch with 3 s timeout; cache in `localStorage` (5 min TTL). Render skeleton → data → stale-while-revalidate feel.  
- **Telemetry**: Fire lightweight, non-blocking `signal_impression` once per mount (no user data).  
- **Type safety + actionability**: Use TypeScript with explicit `Insight` shape and optional `action` link/button.  
- **Single source of truth for UI**: One canonical component, one CSS-in-JS-friendly CSS module, one fetch service.

---

## 2) Data contract (canonical)

```json
// public/data/fallback-top-hub.json
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "connections": 1284,
  "score": 0.94,
  "insights": [
    {
      "title": "Cross‑cloud egress +22%",
      "summary": "Driven by log replication to DR region in last 7 days.",
      "impact": "high",
      "action": "/costs/egress?period=7d"
    },
    {
      "title": "Idle GPU capacity 34%",
      "summary": "Nodes in namespace 'batch-ml' averaging <10% utilization over 14 days.",
      "impact": "high",
      "action": "/infrastructure/namespaces/batch-ml"
    },
    {
      "title": "12 RI recommendations pending",
      "summary": "Estimated annual savings $214k if approved.",
      "impact": "medium",
      "action": "/savings/ris"
    }
  ],
  "updatedAt": "2026-04-29T00:00:00.000Z"
}
```

---

## 3) CDN fetch service (timeout, cache, zero-auth)

```ts
// src/services/cdnService.ts
const CDN_BASE =
  'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/top-hub.json';
const CACHE_TTL_MS = 5 * 60 * 1000; // 5m
const TIMEOUT_MS = 3000;

export interface Insight {
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  action?: string;
}

export interface HubData {
  hub: string;
  label: string;
  connections?: number;
  score?: number;
  insights: Insight[];
  updatedAt: string;
}

export async function fetchTopHub(): Promise<HubData> {
  const cacheKey = 'costinel:top-hub:v2';

  // 1) Try cache
  try {
    const raw = localStorage.getItem(cacheKey);
    if (raw) {
      const { data, ts } = JSON.parse(raw);
      if (Date.now() - ts < CACHE_TTL_MS) return data;
    }
  } catch {
    // ignore cache errors
  }

  // 2) Fetch CDN
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const res = await fetch(CDN_BASE, {
      signal: controller.signal,
      cache: 'no-store',
    });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`CDN ${res.status}`);
    const data = (await res.json()) as HubData;
    try {
      localStorage.setItem(
        cacheKey,
        JSON.stringify({ data, ts: Date.now() })
      );
    } catch {
      // ignore quota errors
    }
    return data;
  } catch {
    clearTimeout(timer);
    // 3) Fallback bundled file
    try {
      const fb = await fetch('/data/fallback-top-hub.json', {
        cache: 'reload',
      });
      if (fb.ok) return (await fb.json()) as HubData;
    } catch {
      // 4) Hardcoded emergency fallback
      return {
        hub: 'MOC',
        label: 'Mission Operations Center',
        connections: 0,
        score: 0,
        insights: [
          {
            title: 'Insights unavailable',
            summary: 'Using default signals. Check CDN connectivity.',
            impact: 'medium',
          },
        ],
        updatedAt: new Date().toISOString(),
      };
    }
  }
}
```

---

## 4) Component (TypeScript, React, CSS module)

```tsx
// src/components/TopHubSignalPanel/TopHubSignalPanel.tsx
import React, { useEffect, useState } from 'react';
import { fetchTopHub, HubData, Insight } from '../../services/cdnService';
import styles from './TopHubSignalPanel.module.css';

const impactColor: Record<Insight['impact'], string> = {
  high: '#ef4444',
  medium: '#f59e0b',
  low: '#10b981',
};

export default function TopHubSignalPanel() {
  const [data, setData] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHub()
      .then((d) => {
        if (mounted) setData(d);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    // Non-blocking telemetry
    try {
      navigator.sendBeacon?.(
        '/telemetry',
        JSON.stringify({ event: 'signal_impression', name: 'top_hub' })
      );
    } catch {
      // ignore
    }

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className={styles.panel} aria-busy="true">
        <div className={styles.shimmer} />
      </div>
    );
  }

  if (!data) return null;

  return (
    <div className={styles.panel} role="region" aria-label="Top hub signal">
      <div className={styles.header}>
        <div>
          <span className={styles.badge}>TOP HUB</span>
          <h3 className={styles.title}>{data.hub}</h3>
          <p className={styles.sub}>{data.label}</p>
        </div>
        <div className={styles.score} title="Connectivity score">
          {Math.round((data.score || 0) * 100)}%
        </div>
      </div>

      <ul className={styles.insights}>
        {data.insights.slice(0, 3).map((insight, i) => (
          <li key={i} className={styles.insight}>
            <span
              className={styles.dot}
              style={{ background: impactColor[insight.impact] }}
            />
            <div className={styles.insightText}>
              <strong>{insight.title}</strong>
              <p>{insight.summary}</p>
              {insight.action && (
                <a href={insight.action} className={styles.action}>
                  View details
                </a>
              )}
            </div>
          </li>
        ))}
      </ul>

      <div className={styles.footer}>
        <small>
          Updated {data.updatedAt ? new Date(data.updatedAt).toLocaleDateString() : '—'}
        </small>
      </div>
    </div>
  );
}
```

```css
/* src/components/TopHubSignalPanel/TopHubSignalPanel.module.css */
.panel {
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  border: 1px
