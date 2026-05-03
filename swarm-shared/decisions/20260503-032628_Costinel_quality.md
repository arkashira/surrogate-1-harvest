# Costinel / quality

**Final Implementation Plan — Top-Hub Signal Panel (CDN-first, production-ready, <2h)**

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces **high-cost anomalies and idle-resource (RI coverage) gaps** as **actionable signals** (Sense + Signal only; no execute)
- Uses **CDN-first fetching** to bypass HuggingFace API rate limits
- **Graceful fallback**: cached summary → static fallback JSON → empty state (never throws)
- **Performance**: renders non-blocking in **<100ms**, never blocks dashboard interactivity

---

### Architecture (CDN-first, fits existing patterns)
- **Data layer**: CDN-only fetches  
  `https://huggingface.co/datasets/axentx/costinel/resolve/main/batches/mirror-merged/{date}/top-hub-signals.json`
- **Cache layer**:  
  - Client: `stale-while-revalidate` via `localStorage` (5min TTL)  
  - Server (fallback route): in-memory LRU (100 items, 5min TTL)
- **Static fallback**: `public/signals/fallback/top-hub.json` (committed)
- **Delivery**:  
  - Server-side route (`/api/signals/cached`) for fallback  
  - Client component hydrates via `fetch` with `AbortController(3s timeout)`
- **Error boundary**: silent degradation; panel never blocks UI

---

### File Changes (3 files, ~130 lines total)

#### 1) `src/components/TopHubSignalPanel.tsx` (new)
```tsx
'use client';
import { useEffect, useState } from 'react';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Cloud, AlertTriangle, CheckCircle, TrendingUp } from 'lucide-react';

interface Signal {
  type: 'anomaly' | 'ri_gap' | 'savings';
  severity: 'high' | 'medium' | 'low';
  service: string;
  region: string;
  current: number;
  projected: number;
  recommendation: string;
  slug: string;
}

const CDN_URL_ROOT = 'https://huggingface.co/datasets/axentx/costinel/resolve/main/batches/mirror-merged';
const FALLBACK_API = '/api/signals/cached';
const LOCAL_KEY = 'topHubSignalsCache';
const TTL_MS = 5 * 60 * 1000;

function isCacheValid(ts: number) {
  return Date.now() - ts < TTL_MS;
}

export function TopHubSignalPanel() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);

    async function fetchSignals() {
      try {
        // 1) Try CDN-first (bypass API limits)
        const date = new Date().toISOString().slice(0, 10);
        const url = `${CDN_URL_ROOT}/${date}/top-hub-signals.json`;

        const res = await fetch(url, { cache: 'no-store', signal: controller.signal });
        if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);

        const data = await res.json();
        const payload = Array.isArray(data) ? data : data.signals || [];

        if (mounted) {
          setSignals(payload);
          setError(null);
          localStorage.setItem(LOCAL_KEY, JSON.stringify({ ts: Date.now(), payload }));
        }
        return;
      } catch (err) {
        // continue to fallback chain
      }

      // 2) Try localStorage stale-while-revalidate cache
      try {
        const raw = localStorage.getItem(LOCAL_KEY);
        if (raw) {
          const { ts, payload } = JSON.parse(raw);
          if (isCacheValid(ts) && Array.isArray(payload)) {
            if (mounted) {
              setSignals(payload);
              setError(null);
            }
            // still try API fallback silently in background
          }
        }
      } catch {
        // ignore malformed cache
      }

      // 3) Try server fallback route
      try {
        const res = await fetch(FALLBACK_API, { cache: 'no-store', signal: controller.signal });
        if (res.ok) {
          const data = await res.json();
          const payload = Array.isArray(data) ? data : data.signals || [];
          if (mounted) {
            setSignals(payload);
            setError(null);
          }
          return;
        }
      } catch {
        // ignore
      }

      // 4) Final: static fallback bundled in repo (public/)
      try {
        const res = await fetch('/signals/fallback/top-hub.json', { cache: 'no-store', signal: controller.signal });
        if (res.ok) {
          const payload = await res.json();
          if (mounted) {
            setSignals(Array.isArray(payload) ? payload : payload.signals || []);
            setError(null);
          }
          return;
        }
      } catch {
        // silent fail
      }

      if (mounted) setError('Using cached data; signals may be stale');
    } finally {
      if (mounted) {
        setLoading(false);
        clearTimeout(timeout);
      }
    }

    fetchSignals();
    return () => {
      mounted = false;
      controller.abort();
      clearTimeout(timeout);
    };
  }, []);

  const getSeverityColor = (severity: string) => {
    switch (severity) {
      case 'high': return 'text-red-600 bg-red-50';
      case 'medium': return 'text-amber-600 bg-amber-50';
      default: return 'text-blue-600 bg-blue-50';
    }
  };

  const getIcon = (type: string) => {
    switch (type) {
      case 'anomaly': return <AlertTriangle className="h-4 w-4" />;
      case 'ri_gap': return <Cloud className="h-4 w-4" />;
      case 'savings': return <CheckCircle className="h-4 w-4" />;
      default: return <TrendingUp className="h-4 w-4" />;
    }
  };

  if (loading) {
    return (
      <Card className="p-4 animate-pulse">
        <div className="h-4 bg-gray-200 rounded w-3/4 mb-2"></div>
        <div className="h-3 bg-gray-200 rounded w-1/2"></div>
      </Card>
    );
  }

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-semibold text-gray-900">Top-Hub Signals</h3>
        <Badge variant="outline" className="text-xs">
          {signals.length} active
        </Badge>
      </div>

      {error && (
        <div className="text-xs text-amber-600 mb-2 flex items-center gap-1">
          <AlertTriangle className="h-3 w-3" />
          {error}
        </div>
      )}

      <div className="space-y-3">
        {signals.length === 0 ? (
          <p className="text-sm text-gray-500 text-center py-4">No active signals</p>
        ) : (
          signals.slice(0, 5).map((signal, i) => (
            <div
              key={`${signal.slug}-${i}`}
              className="flex items-start gap-3 p-2 rounded-lg border hover:bg-gray-50 transition-colors"
            >
              <div className={`p-1.5 rounded ${getSeverityColor(signal.severity)}`}>
                {getIcon(signal.type)}
              </div>
              <div className="flex-1 min-w-
