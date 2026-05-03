# Costinel / backend

## Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. CDN-first data path; zero backend changes; ships in <2h.

**Why this is highest-value incremental**  
- Directly applies past lessons: review most-connected hub (MOC) before planning; surfaces actionable signals without execution.  
- Minimal surface area (read-only panel + static JSON) → low risk, immediate visibility for governance workflows.  
- Enables downstream “Sense + Signal” UX in Costinel without touching execution paths.

---

### File changes
- `src/components/dashboard/TopHubSignalPanel.tsx` — new component  
- `src/components/dashboard/index.ts` — export addition  
- `public/data/knowledge-graph/moc-hub.json` — static payload (CDN-friendly)  
- `src/pages/Dashboard.tsx` — mount panel in existing layout (high-impact zone)

---

### Data contract (CDN-first)
Path: `public/data/knowledge-graph/moc-hub.json`

```json
{
  "hub": {
    "id": "MOC",
    "label": "Mission Operations Center",
    "description": "Most-connected hub for cloud cost governance signals",
    "importance": 0.94,
    "connectedNodes": 127,
    "lastUpdated": "2026-05-03T02:14:13.000Z"
  },
  "proposals": [
    {
      "id": "prop-001",
      "title": "Shift 30% of dev workloads to Savings Plans",
      "impactUsd": 18400,
      "confidence": 0.87,
      "timeframe": "Q3-2026",
      "tags": ["RI", "AWS", "dev"],
      "rationale": "Low-utilization on-demand patterns detected across 42 accounts"
    },
    {
      "id": "prop-002",
      "title": "Delete unattached EBS volumes (est. 2.1 TB)",
      "impactUsd": 310,
      "confidence": 0.92,
      "timeframe": "Immediate",
      "tags": ["cleanup", "EBS", "AWS"],
      "rationale": "Orphaned volumes older than 30 days with zero attachments"
    },
    {
      "id": "prop-003",
      "title": "Right-size over-provisioned GKE node pools",
      "impactUsd": 4200,
      "confidence": 0.78,
      "timeframe": "2 weeks",
      "tags": ["GCP", "Kubernetes", "rightsize"],
      "rationale": "CPU requests exceed usage by 2.4x on average"
    }
  ]
}
```

---

### Component implementation

`src/components/dashboard/TopHubSignalPanel.tsx`

```tsx
import React, { useEffect, useState } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertCircle, TrendingUp, Tag } from "lucide-react";

type Proposal = {
  id: string;
  title: string;
  impactUsd: number;
  confidence: number;
  timeframe: string;
  tags: string[];
  rationale: string;
};

type HubData = {
  hub: {
    id: string;
    label: string;
    description: string;
    importance: number;
    connectedNodes: number;
    lastUpdated: string;
  };
  proposals: Proposal[];
};

const TopHubSignalPanel: React.FC = () => {
  const [data, setData] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // CDN-first fetch (no auth, bypasses API rate limits)
    fetch("/data/knowledge-graph/moc-hub.json", { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load hub data: ${res.status}`);
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-48" />
        </CardHeader>
        <CardContent className="space-y-4">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2 text-destructive">
            <AlertCircle className="h-5 w-5" />
            <CardTitle className="text-base">Unable to load signals</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{error}</p>
        </CardContent>
      </Card>
    );
  }

  if (!data) return null;

  const formatUSD = (n: number) =>
    new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 0,
    }).format(n);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="text-base font-semibold flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-primary" />
              {data.hub.label}
            </CardTitle>
            <p className="text-xs text-muted-foreground mt-1">
              {data.hub.description}
            </p>
          </div>
          <Badge variant="secondary" className="text-xs">
            {data.hub.connectedNodes} nodes
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        {data.proposals.slice(0, 3).map((p) => (
          <div
            key={p.id}
            className="flex flex-col gap-1.5 p-3 rounded-md border bg-muted/30"
          >
            <div className="flex items-start justify-between gap-2">
              <p className="text-sm font-medium leading-tight">{p.title}</p>
              <span className="text-xs font-semibold text-success whitespace-nowrap">
                {formatUSD(p.impactUsd)}
              </span>
            </div>

            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>Confidence {(p.confidence * 100).toFixed(0)}%</span>
              <span>•</span>
              <span>{p.timeframe}</span>
            </div>

            <div className="flex flex-wrap gap-1 pt-1">
              {p.tags.map((t) => (
                <Badge
                  key={t}
                  variant="outline"
                  className="text-[10px] px-1.5 py-0.5"
                >
                  <Tag className="h-2.5 w-2.5 mr-0.5 opacity-60" />
                  {t}
                </Badge>
              ))}
            </div>

            <p className="text-xs text-muted-foreground line-clamp-2 mt-1">
              {p.rationale}
            </p>
          </div>
        ))}

        <p className="text-[1
