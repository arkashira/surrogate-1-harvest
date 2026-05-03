# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel  
*CDN-first, non-blocking, <2h, production-ready*

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces the most-connected hub (e.g., `"MOC"`) from the knowledge-rag graph  
- Uses **CDN-first static delivery** to bypass HF API rate limits and avoid blocking dashboard render  
- Presents contextual cost-governance **signals as proposals only** (Sense + Signal — ไม่ Execute)  
- Graceful fallback to cached/empty state; zero impact on main UI or execution paths  

---

### Architecture (fits existing patterns)
```
Costinel Dashboard (React/Next.js)
  ├── public/data/top-hub.json        # CDN-bypass payload (static, no auth)
  ├── components/TopHubSignalPanel.tsx
  ├── scripts/regenerate-top-hub.sh   # ops helper (bash + curl)
  └── api/proposals (POST)            # audit trail for proposals
```

- **Data source**: knowledge-rag graph export → `public/data/top-hub.json` (committed via CI or ops script)  
- **Delivery**: static CDN file (bypasses HF API; served with proper cache headers)  
- **Client**: lightweight React panel hydrates from CDN JSON, polls refresh every 300s (non-blocking)  
- **Philosophy**: proposals only; no auto-apply actions  

---

### 1) CDN payload schema (public/data/top-hub.json)
```json
{
  "hub": "MOC",
  "connections": 142,
  "category": "cost-governance",
  "signals": [
    {
      "id": "SIG-2026-05-03-001",
      "type": "anomaly",
      "severity": "high",
      "title": "Reserved Instance Coverage Gap",
      "description": "us-east-1 m5.xlarge RI coverage at 42% — opportunity for 38% savings",
      "context": {
        "service": "EC2",
        "region": "us-east-1",
        "instance_type": "m5.xlarge",
        "current_coverage": 0.42,
        "recommended_coverage": 0.80,
        "estimated_savings_usd": 1247.5
      },
      "proposal": {
        "action": "purchase_ri",
        "scope": "us-east-1/m5.xlarge",
        "quantity": 45,
        "term": "1yr_no_upfront",
        "confidence": 0.87
      },
      "timestamp": "2026-05-03T03:24:34Z"
    }
  ],
  "last_updated": "2026-05-03T03:24:34Z",
  "ttl": 300
}
```

---

### 2) TopHubSignalPanel component (React, non-blocking)
```tsx
// src/components/TopHubSignalPanel.tsx
'use client';

import { useEffect, useState } from 'react';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { AlertCircle, TrendingUp, TrendingDown } from 'lucide-react';

interface Signal {
  id: string;
  type: 'anomaly' | 'opportunity' | 'warning';
  severity: 'high' | 'medium' | 'low';
  title: string;
  description: string;
  context: Record<string, any>;
  proposal: {
    action: string;
    scope: string;
    confidence: number;
  };
  timestamp: string;
}

interface TopHubData {
  hub: string;
  connections: number;
  category: string;
  signals: Signal[];
  last_updated: string;
}

const FALLBACK_DATA: TopHubData = {
  hub: 'MOC',
  connections: 142,
  category: 'cost-governance',
  signals: [
    {
      id: 'SIG-FALLBACK',
      type: 'warning',
      severity: 'medium',
      title: 'Signal service unavailable',
      description: 'Using cached recommendations',
      context: {},
      proposal: { action: 'monitor', scope: 'all', confidence: 0.5 },
      timestamp: new Date().toISOString()
    }
  ],
  last_updated: new Date().toISOString()
};

export function TopHubSignalPanel() {
  const [hubData, setHubData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchHubData = async () => {
    try {
      // CDN-first fetch (bypasses API rate limits)
      const res = await fetch('/data/top-hub.json', { cache: 'no-store' });
      if (!res.ok) throw new Error('CDN fetch failed');
      const data = await res.json();
      setHubData(data);
    } catch {
      // Graceful fallback; non-blocking: fail silently in UI
      setHubData(FALLBACK_DATA);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchHubData();
    const interval = setInterval(fetchHubData, 300000); // 5min refresh
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return <div className="h-32 animate-pulse bg-muted rounded-lg" />;
  }

  const severityColor = (s: string) => {
    switch (s) {
      case 'high': return 'bg-red-100 text-red-800';
      case 'medium': return 'bg-yellow-100 text-yellow-800';
      default: return 'bg-blue-100 text-blue-800';
    }
  };

  const signalIcon = (type: string) => {
    switch (type) {
      case 'anomaly': return <AlertCircle className="h-4 w-4" />;
      case 'opportunity': return <TrendingUp className="h-4 w-4" />;
      default: return <TrendingDown className="h-4 w-4" />;
    }
  };

  return (
    <Card className="p-4 mb-6 border-l-4 border-l-blue-500">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="font-semibold">Top Hub: {hubData?.hub}</span>
          <Badge variant="outline">{hubData?.connections} connections</Badge>
        </div>
        <Badge variant="secondary" className="text-xs">
          {hubData?.category}
        </Badge>
      </div>

      <div className="space-y-3">
        {hubData?.signals.map((signal) => (
          <div key={signal.id} className="p-3 bg-muted rounded-lg">
            <div className="flex items-start justify-between mb-2">
              <div className="flex items-center gap-2">
                {signalIcon(signal.type)}
                <span className="font-medium text-sm">{signal.title}</span>
                <Badge className={severityColor(signal.severity)}>
                  {signal.severity}
                </Badge>
              </div>
            </div>
            <p className="text-sm text-muted-foreground mb-2">
              {signal.description}
            </p>
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>Confidence: {(signal.proposal.confidence * 100).toFixed(0)}%</span>
              <span>{new Date(signal.timestamp).toLocaleString()}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-3 pt-3 border-t text-xs text-muted-foreground">
        Sense + Signal — ไม่ Execute | Last sync:{' '}
        {new Date(hubData?.last_updated || '').toLocaleString()}
      </div>
    </Card>
  );
}
```


