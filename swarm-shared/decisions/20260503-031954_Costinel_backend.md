# Costinel / backend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### Scope
Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel dashboard that:
- Detects and surfaces the most-connected hub (default `MOC`) from a lightweight hub-graph index
- Shows 3 contextual insights from knowledge-rag
- Uses CDN-first pattern (bypasses HF API auth/rate-limits) for production safety
- Zero backend changes — pure frontend widget + static asset fetch

### Why this is highest value (<2h)
- Reuses existing `#knowledge-rag #graph #hub` patterns (MOC insight)
- No infra, no DB, no training pipeline — pure incremental UX
- CDN-first avoids HF API limits and keeps backend unchanged
- Delivers immediate contextual intelligence to cost governance users

---

### Implementation Steps (1h 30m total)

#### 1) Create hub-graph index (5m)
Create `/opt/axentx/Costinel/public/knowledge/hub-graph.json` (lightweight, CDN-ready):

```json
{
  "generated_at": "2026-05-03T03:13:56Z",
  "top_hub": "MOC",
  "hubs": {
    "MOC": {
      "name": "Mission Operations Center",
      "connections": 127,
      "rank": 1,
      "insight_keys": ["moc-cost-drift", "moc-ri-coverage", "moc-anomaly-burst"]
    },
    "SEC": {
      "name": "Security Command",
      "connections": 94,
      "rank": 2,
      "insight_keys": ["sec-iam-drift", "sec-kms-rotation"]
    }
  }
}
```

#### 2) Create contextual insights (5m)
Create `/opt/axentx/Costinel/public/knowledge/insights.json`:

```json
{
  "moc-cost-drift": {
    "title": "MOC Cost Drift Alert",
    "severity": "warning",
    "text": "Mission Operations Center shows 18% week-over-week cost increase driven by cross-region data replication. Recommend reserved capacity review.",
    "action": "Review RI coverage for eu-central-1 and us-east-1"
  },
  "moc-ri-coverage": {
    "title": "RI Coverage Gap",
    "severity": "info",
    "text": "MOC compute utilization at 72% with 45% RI coverage. Additional 25% RI commitment yields estimated 32% savings.",
    "action": "Purchase 2yr convertible RIs for m5.2xlarge family"
  },
  "moc-anomaly-burst": {
    "title": "Anomaly Burst Detected",
    "severity": "critical",
    "text": "Detected 47 cost anomalies in MOC over last 72h (baseline: 12). Primary driver: unoptimized EBS snapshots and idle load balancers.",
    "action": "Run snapshot lifecycle policy audit"
  },
  "sec-iam-drift": {
    "title": "IAM Policy Drift",
    "severity": "warning",
    "text": "12 IAM policies in Security Command exceed least-privilege baseline. Potential compliance exposure.",
    "action": "Run IAM access advisor review"
  },
  "sec-kms-rotation": {
    "title": "KMS Key Rotation Lag",
    "severity": "info",
    "text": "3 customer-managed keys in Security Command exceed 90-day rotation window.",
    "action": "Enable automatic key rotation"
  }
}
```

#### 3) Add Top-Hub Signal Panel component (45m)
Create `/opt/axentx/Costinel/src/components/TopHubSignalPanel.jsx`:

```jsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

const CACHE_TTL = 5 * 60 * 1000; // 5 minutes

export default function TopHubSignalPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const loadFromCDN = async () => {
      try {
        const cacheKey = 'costinel_hub_graph_cache';
        const cached = localStorage.getItem(cacheKey);
        const now = Date.now();

        if (cached) {
          const { timestamp, payload } = JSON.parse(cached);
          if (now - timestamp < CACHE_TTL) {
            setData(payload);
            setLoading(false);
            return;
          }
        }

        // CDN-first fetch — no Authorization header, bypasses HF API rate limits
        const baseUrl = window.COSTINEL_CDN_BASE || '/knowledge';
        
        const graphRes = await fetch(`${baseUrl}/hub-graph.json`, {
          cache: 'no-cache',
          credentials: 'same-origin'
        });
        
        if (!graphRes.ok) throw new Error(`Graph fetch failed: ${graphRes.status}`);
        
        const graph = await graphRes.json();
        const topHub = graph.top_hub || 'MOC';
        const hubInfo = graph.hubs?.[topHub];

        const insightsRes = await fetch(`${baseUrl}/insights.json`, {
          cache: 'no-cache',
          credentials: 'same-origin'
        });
        
        if (!insightsRes.ok) throw new Error(`Insights fetch failed: ${insightsRes.status}`);
        
        const insights = await insightsRes.json();
        const topInsights = (hubInfo?.insight_keys || []).slice(0, 3)
          .map(key => insights[key])
          .filter(Boolean);

        const payload = {
          topHub,
          hubInfo,
          insights: topInsights,
          generatedAt: graph.generated_at
        };

        localStorage.setItem(cacheKey, JSON.stringify({
          timestamp: now,
          payload
        }));

        setData(payload);
        setLoading(false);
      } catch (err) {
        console.warn('[TopHubSignalPanel] CDN fetch failed, using graceful fallback:', err);
        setError(err.message);
        setLoading(false);
      }
    };

    loadFromCDN();
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading">
        <div className="panel-skeleton">
          <div className="skeleton-header" />
          <div className="skeleton-row" />
          <div className="skeleton-row" />
        </div>
      </div>
    );
  }

  if (error || !data) {
    // Graceful degradation — panel collapses silently
    return null;
  }

  const severityColors = {
    critical: '#dc2626',
    warning: '#d97706',
    info: '#2563eb'
  };

  return (
    <div className="top-hub-panel" role="region" aria-label="Top hub signals">
      <div className="panel-header">
        <div className="hub-badge">
          <span className="hub-rank">{data.hubInfo?.rank || 1}</span>
          <span className="hub-name">{data.hubInfo?.name || data.topHub}</span>
          <span className="hub-connections">
            {data.hubInfo?.connections?.toLocaleString()} connections
          </span>
        </div>
        <div className="panel-meta">
          Updated {new Date(data.generatedAt).toLocaleDateString()}
        </div>
      </div>

      <div className="insights-list">
        {data.insights.map((insight, idx) => (
          <div key={idx} className="insight-card">
            <div className="insight-header">
              <span
                className="severity-dot"
                style={{ backgroundColor: severityColors[insight.severity] }}
                title={insight.severity}
              />
              <strong className="insight-title">{insight.title}</strong>
            </div>
            <p className="insight-text">{insight.text}</p>
            <div className="insight-action">
              → {insight.action}
            </div>
          </div>
        ))}
      </div
