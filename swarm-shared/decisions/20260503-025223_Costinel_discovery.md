# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard sidebar/top area.
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short description, top 5 signals (cards), last-updated timestamp.
- **CDN-first data path**: fetches from  
  `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/hubs/{hubName}/latest.json`  
  (no auth, bypasses HF API rate limits).
- Telemetry-aware: emits lightweight `panel_impression` and `signal_click` events via `window.axentxTelemetry` if present; no-op fallback.
- Graceful failure: if CDN fails or returns malformed data, panel collapses to minimal state with a retry button (no page breakage).

---

### Files to modify/create
- `src/components/TopHubSignalPanel.tsx` — new React component.
- `src/components/TopHubSignalPanel.module.css` — minimal scoped styles.
- `src/pages/Dashboard.tsx` (or `src/dashboard/Dashboard.tsx`) — import and mount panel.
- `.env.local` — optional `VITE_HUB_NAME=MOC`.

---

### Implementation (≤2h)

#### 1) Environment (optional)
```env
# .env.local
VITE_HUB_NAME=MOC
```

#### 2) Scoped CSS
```css
/* src/components/TopHubSignalPanel.module.css */
.panel {
  border-radius: 0.5rem;
  background: #fff;
  border: 1px solid #e6e6e6;
  box-shadow: 0 1px 2px rgba(0,0,0,0.05);
  overflow: hidden;
}

.header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #f0f0f0;
}

.title {
  font-size: 0.875rem;
  font-weight: 600;
  color: #111;
  margin: 0;
}

.subtitle {
  font-size: 0.75rem;
  color: #6b7280;
  margin: 0.125rem 0 0 0;
}

.updated {
  font-size: 0.6875rem;
  color: #9ca3af;
  white-space: nowrap;
}

.body {
  padding: 0.25rem 0;
}

.signalCard {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
  padding: 0.625rem 1rem;
  cursor: pointer;
  transition: background-color 0.12s ease;
}

.signalCard:hover {
  background-color: #f9fafb;
}

.signalCardContent {
  flex: 1 1 0%;
  min-width: 0;
}

.signalTitle {
  font-size: 0.8125rem;
  font-weight: 500;
  color: #111827;
  margin: 0;
  line-height: 1.3;
  word-break: break-word;
}

.signalSummary {
  font-size: 0.75rem;
  color: #6b7280;
  margin: 0.25rem 0 0 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.35;
}

.signalAction {
  font-size: 0.75rem;
  color: #2563eb;
  margin: 0.25rem 0 0 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.severity {
  flex: 0 0 auto;
  font-size: 0.625rem;
  font-weight: 600;
  padding: 0.1875rem 0.375rem;
  border-radius: 9999px;
  text-transform: capitalize;
  white-space: nowrap;
}

.severityCritical { background: #fee2e2; color: #991b1b; }
.severityHigh     { background: #fed7aa; color: #9a3412; }
.severityMedium   { background: #fef3c7; color: #92400e; }
.severityLow      { background: #f3f4f6; color: #374151; }

.footer {
  padding: 0.375rem 1rem;
  border-top: 1px solid #f3f4f6;
  font-size: 0.625rem;
  color: #9ca3af;
  text-align: center;
}

.loading {
  padding: 1rem;
}

.skeleton {
  background: #f3f4f6;
  border-radius: 0.25rem;
  animation: pulse 1s ease-in-out infinite;
}

@keyframes pulse {
  0%,100% { opacity: 1; }
  50% { opacity: 0.6; }
}

.error {
  padding: 0.75rem 1rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.errorText {
  font-size: 0.75rem;
  color: #92400e;
}

.retryLink {
  font-size: 0.75rem;
  color: #92400e;
  text-decoration: underline;
  cursor: pointer;
}

.empty {
  padding: 0.75rem 1rem;
  font-size: 0.75rem;
  color: #9ca3af;
  text-align: center;
}
```

#### 3) Component
```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState, useCallback } from 'react';
import styles from './TopHubSignalPanel.module.css';

const HUB_NAME = import.meta.env.VITE_HUB_NAME || 'MOC';
const CDN_URL = `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/hubs/${encodeURIComponent(HUB_NAME)}/latest.json`;

interface Signal {
  id: string;
  title: string;
  summary: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  action?: string;
}

interface HubData {
  title?: string;
  description?: string;
  signals?: Signal[];
  updatedAt?: string;
}

function emitTelemetry(event: string, payload?: Record<string, unknown>) {
  try {
    if (typeof window !== 'undefined' && (window as any).axentxTelemetry) {
      (window as any).axentxTelemetry(event, payload);
    }
  } catch {
    // no-op
  }
}

function formatDate(iso?: string) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  } catch {
    return '—';
  }
}

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchHub = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(CDN_URL,
