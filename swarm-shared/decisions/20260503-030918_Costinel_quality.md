# Costinel / quality

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

**Scope**: Add a lightweight, non-blocking **Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default: `MOC`) with contextual insights from the knowledge-rag graph. Uses CDN-first static JSON to avoid API rate limits and keep the change under 2 hours.

### Architecture (CDN-first)
- **Data source**: `public/hubs/top-hub.json` (committed artifact from `knowledge-rag` runs)
- **Runtime**: Dashboard fetches `/hubs/top-hub.json` with `no-store` cache-bust; fails silently if 404/timeout
- **No backend changes** — pure frontend addition, deployable via existing static pipeline
- **Tags**: #knowledge-rag #graph #hub #cdn

---

### File changes

#### 1) `public/hubs/top-hub.json` (seed/example)
```json
{
  "hubId": "MOC",
  "label": "Mission Operations Center",
  "score": 94,
  "connections": 1273,
  "updated": "2026-04-27T14:30:00Z",
  "insights": [
    "Top cross-team dependency — 34% of cost anomalies trace to MOC-linked services",
    "Recommended: apply tagging policy v2 to MOC-owned accounts (est. 8–12% savings)",
    "Recent spike in egress from MOC → prod-analytics (review VPC endpoints)"
  ],
  "signals": [
    {
      "id": "si-001",
      "type": "anomaly",
      "severity": "high",
      "title": "Unattached EBS surge in us-east-1",
      "context": "Detected 47 unattached gp3 volumes (~$310/mo) across dev accounts.",
      "action": "Review unattached-volume proposal"
    }
  ],
  "tags": ["#knowledge-rag", "#graph", "#hub", "#MOC"]
}
```

#### 2) `src/components/TopHubSignalPanel.tsx` (new)
```tsx
import { useEffect, useState } from "react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { AlertCircle, ExternalLink, TrendingUp } from "lucide-react";

interface Signal {
  id: string;
  type: string;
  severity: "low" | "medium" | "high" | "critical";
  title: string;
  context: string;
  action: string;
}

interface TopHubData {
  hubId: string;
  label: string;
  score: number;
  connections: number;
  updated: string;
  insights: string[];
  signals: Signal[];
  tags: string[];
}

export function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    // CDN-first fetch; no Authorization header required
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);

    fetch("/hubs/top-hub.json?ts=" + Date.now(), {
      signal: controller.signal,
      cache: "no-store",
    })
      .then((res) => {
        if (!res.ok) throw new Error("Not found");
        return res.json();
      })
      .then((json) => {
        setData(json);
        setError(false);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false))
      .finally(() => clearTimeout(timeout));

    return () => controller.abort();
  }, []);

  if (loading) {
    return (
      <Card className="p-4 opacity-60">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <TrendingUp className="h-4 w-4" />
          Loading top-hub signal...
        </div>
      </Card>
    );
  }

  if (error || !data) {
    // Non-blocking graceful fallback
    return null;
  }

  const severityColors = {
    low: "bg-blue-100 text-blue-800 border-blue-200",
    medium: "bg-yellow-100 text-yellow-800 border-yellow-200",
    high: "bg-orange-100 text-orange-800 border-orange-200",
    critical: "bg-red-100 text-red-800 border-red-200",
  };

  return (
    <Card className="p-4 border-l-4 border-l-primary bg-card/70">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <TrendingUp className="h-4 w-4 text-primary" />
            <span className="font-semibold text-sm">Top-Hub Signal</span>
            <Badge variant="secondary" className="text-xs">
              {data.hubId}
            </Badge>
          </div>
          <p className="text-sm font-medium text-foreground truncate">
            {data.label}
          </p>
          <p className="text-xs text-muted-foreground mt-0.5">
            {data.connections.toLocaleString()} connections · Score {data.score}
          </p>

          <div className="mt-3 space-y-1.5">
            {data.insights.slice(0, 2).map((insight, idx) => (
              <p key={idx} className="text-sm text-muted-foreground line-clamp-2">
                • {insight}
              </p>
            ))}
          </div>

          {data.signals && data.signals.length > 0 && (
            <div className="mt-4 space-y-2">
              <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                Active Signals
              </p>
              {data.signals.slice(0, 2).map((signal) => (
                <div
                  key={signal.id}
                  className="p-2 rounded-md border bg-card/50 text-xs"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 mb-1">
                        <span
                          className={`inline-flex h-2 w-2 rounded-full ${
                            severityColors[signal.severity]
                          }`}
                        />
                        <span className="font-medium text-foreground">
                          {signal.title}
                        </span>
                      </div>
                      <p className="text-muted-foreground line-clamp-1">
                        {signal.context}
                      </p>
                    </div>
                  </div>
                  <div className="mt-1.5">
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary font-medium">
                      {signal.action}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="flex items-center gap-1.5 mt-3 flex-wrap">
            {data.tags.slice(0, 3).map((tag) => (
              <Badge key={tag} variant="outline" className="text-[10px] px-1.5 py-0.5">
                {tag}
              </Badge>
            ))}
          </div>

          <p className="text-[10px] text-muted-foreground/60 mt-2">
            Updated {new Date(data.updated).toLocaleDateString()}
          </p>
        </div>
      </div>
    </Card>
  );
}
```

#### 3) Integrate into dashboard (`src/pages/dashboard.tsx` or equivalent)
Locate the main dashboard grid and insert the panel near the top (below header or in a sidebar/aside). Example insertion:

