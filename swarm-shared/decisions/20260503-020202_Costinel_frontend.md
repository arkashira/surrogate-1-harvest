# Costinel / frontend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

### Scope & Value
- **Frontend-only, read-only React panel** that surfaces the highest-signal hub (default “MOC”) and its top actionable cost-saving proposals from the knowledge graph.
- **CDN-first data delivery** to eliminate HF API rate limits during dashboard loads.
- **Incremental, non-breaking change** that ships in <2 hours.

---

### 1) Data contract (CDN JSON)

**Path** (canonical):  
`https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/dashboard/top-hub/latest.json`

**Schema** (minimal, production-ready):

```json
{
  "hub": "MOC",
  "title": "Multi-Org Cost Optimization",
  "score": 94.2,
  "updatedAt": "2026-05-03T02:00:58Z",
  "proposals": [
    {
      "id": "moc-ri-2026-05",
      "title": "Convert 32 m5.xlarge running 24/7 to 1-yr RIs",
      "impact": {
        "monthlySavingsUSD": 1840,
        "confidence": 0.87
      },
      "tags": ["AWS", "RI", "prod"],
      "actions": [
        {
          "label": "View analysis",
          "href": "/proposals/moc-ri-2026-05"
        },
        {
          "label": "Create change request",
          "href": "/change-requests/new?proposal=moc-ri-2026-05"
        }
      ]
    }
  ]
}
```

**Key decisions (resolved contradictions):**
- Use `proposals[].title` (more descriptive than “headline”) and `proposals[].impact.monthlySavingsUSD` (clearer semantics than flat `impactUSD`).
- Keep `score` at the hub level to surface overall signal strength.
- Preserve `tags` for filtering/visual badges.
- Provide multiple `actions` (primary + secondary) for immediate workflow movement.

---

### 2) Component: `TopHubSignalPanel`

**Location:** `src/components/dashboard/TopHubSignalPanel.tsx`

```tsx
import { useEffect, useState } from 'react';
import { TrendingUp, AlertCircle, ExternalLink } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';

interface ProposalAction {
  label: string;
  href: string;
}

interface Proposal {
  id: string;
  title: string;
  impact: {
    monthlySavingsUSD: number;
    confidence: number;
  };
  tags: string[];
  actions: ProposalAction[];
}

interface TopHubPayload {
  hub: string;
  title: string;
  score: number;
  updatedAt: string;
  proposals: Proposal[];
}

const CDN_URL =
  'https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/dashboard/top-hub/latest.json';

function formatUSD(value: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(value);
}

export function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchData() {
      try {
        const res = await fetch(CDN_URL, { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = (await res.json()) as TopHubPayload;
        setData(json);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load top-hub signals');
      } finally {
        setLoading(false);
      }
    }

    fetchData();
  }, []);

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-32" />
        </CardHeader>
        <CardContent className="space-y-4">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 py-6 text-sm text-destructive">
          <AlertCircle className="h-4 w-4" />
          Could not load top-hub signals.
        </CardContent>
      </Card>
    );
  }

  if (!data || data.proposals.length === 0) {
    return null;
  }

  const topProposal = data.proposals[0];
  const primaryAction = topProposal.actions[0];
  const secondaryActions = topProposal.actions.slice(1);
  const hasMoreProposals = data.proposals.length > 1;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between pb-3">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <TrendingUp className="h-4 w-4 text-primary" />
            {data.hub}
          </CardTitle>
          <p className="text-xs text-muted-foreground">{data.title}</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="text-xs">
            Score {data.score}
          </Badge>
          <Badge variant="outline" className="text-xs">
            Updated {new Date(data.updatedAt).toLocaleDateString()}
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="rounded-lg border bg-muted/50 p-3">
          <p className="text-sm font-medium">{topProposal.title}</p>
          <div className="mt-2 flex items-center justify-between">
            <span className="text-sm font-semibold text-primary">
              {formatUSD(topProposal.impact.monthlySavingsUSD)}/mo potential savings
            </span>
            <Badge variant="secondary">
              {Math.round(topProposal.impact.confidence * 100)}% confidence
            </Badge>
          </div>
          <div className="mt-2 flex flex-wrap gap-1">
            {topProposal.tags.map((t) => (
              <Badge key={t} variant="outline" className="text-xs">
                {t}
              </Badge>
            ))}
          </div>
          <div className="mt-3 flex items-center gap-2">
            {primaryAction && (
              <Button asChild variant="link" className="h-auto p-0 text-sm" size="sm">
                <a
                  href={primaryAction.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center"
                >
                  {primaryAction.label}
                  <ExternalLink className="ml-1 h-3 w-3" />
                </a>
              </Button>
            )}
            {secondaryActions.length > 0 && (
              <Button asChild variant="ghost" size="sm" className="h-auto p-0 text-sm">
                <a href={secondaryActions[0].href}>{secondaryActions[0].label}</a>
              </Button>
            )}
          </div>
        </div>

        {hasMoreProposals && (
          <Button asChild variant="ghost" size="sm" className="w-full">
            <a href={`/proposals?hub=${encodeURIComponent(data.hub)}`}>
              View all {data.proposals.length} proposals
            </a>
          </Button>
