# Costinel / frontend

## Final Synthesized Implementation (Best of Both Candidates)

**Goal:** Add a **Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals — **zero HuggingFace API calls, CDN-first, cacheable, <2h**.

---

### Key Synthesis Decisions (Resolving Contradictions)

| Contradiction | Resolution (Correctness + Actionability) |
|---------------|------------------------------------------|
| **Static data vs CDN JSON** | Use **CDN JSON** (`public/data/hubs/{hubName}/proposals.json`) as the source of truth (Candidate 2), but keep a **compile-time fallback** in code (Candidate 1) so the panel renders even if the CDN file is missing or during local dev without network. This gives deployability + resilience. |
| **SWR/React-Query vs No fetcher** | Use **`useSWR` with 5-minute stale-while-revalidate** (Candidate 2) for cacheability and fast repeat visits. Add **skeleton loader + empty state** for UX polish. |
| **Env var naming** | Use `VITE_HUB_NAME` (Vite) or `NEXT_PUBLIC_HUB_NAME` (Next) depending on framework — detect and document both. Default `"MOC"`. |
| **Styling approach** | Adopt Candidate 1’s minimal, token-based CSS (easy to integrate) but add **skeleton styles** and **responsive grid** from Candidate 2. |
| **Impact display** | Use Candidate 1’s color-coded impact (`high/medium/low`) **plus** Candidate 2’s monetary/time impact string (e.g., `"Save $12k/mo"`) — show both if available. |

---

### Implementation Plan (<2h)

1. **Create CDN data file**  
   `public/data/hubs/MOC/proposals.json` — static, deployable with repo.

2. **Create component** `src/components/TopHubSignalPanel.tsx`  
   - Uses `useSWR` to fetch CDN JSON.  
   - Falls back to embedded static data if fetch fails or during SSR.  
   - Shows 3 proposal cards with title, impact, action, optional runbook link.  
   - Skeleton loader while loading; empty state if none.

3. **Add minimal CSS** (`TopHubSignalPanel.css`) — tokens, spacing, responsive.

4. **Wire into dashboard** (`src/pages/Dashboard.tsx` or `app/dashboard/page.tsx`)  
   - Place in top row or right sidebar.  
   - Responsive: full width mobile, 1/2–1/3 desktop.

5. **Config via env**  
   - `VITE_HUB_NAME` (Vite) or `NEXT_PUBLIC_HUB_NAME` (Next) — default `"MOC"`.

6. **Test & ship**  
   - Verify CDN fetch works without auth.  
   - Check Lighthouse for CLS, performance.  
   - Commit and deploy.

---

### Code Snippets

#### 1) CDN Data File (`public/data/hubs/MOC/proposals.json`)
```json
{
  "hubName": "MOC",
  "updatedAt": "2025-01-01T00:00:00Z",
  "proposals": [
    {
      "id": "moc-1",
      "title": "Right-size oversized dev instances",
      "impact": "high",
      "impactLabel": "Save $8k/mo",
      "description": "30% of dev workloads run on larger-than-needed instance types.",
      "action": "Apply rightsizing policy to dev/staging accounts.",
      "runbookUrl": "https://wiki.costinel/runbooks/rightsizing-dev"
    },
    {
      "id": "moc-2",
      "title": "Increase RI coverage for steady-state services",
      "impact": "high",
      "impactLabel": "Save $15k/mo",
      "description": "Current RI coverage ~45%; target 75% for predictable workloads.",
      "action": "Purchase 1-year convertible RIs for core services.",
      "runbookUrl": "https://wiki.costinel/runbooks/ri-coverage"
    },
    {
      "id": "moc-3",
      "title": "Enable S3 Intelligent-Tiering for cold data",
      "impact": "medium",
      "impactLabel": "Save $3k/mo",
      "description": "Cold objects in standard tier driving avoidable storage cost.",
      "action": "Apply lifecycle rule to move >30d objects to Intelligent-Tiering.",
      "runbookUrl": "https://wiki.costinel/runbooks/s3-tiering"
    }
  ]
}
```

#### 2) Component (`src/components/TopHubSignalPanel.tsx`)
```tsx
'use client';

import useSWR from 'swr';
import { useMemo } from 'react';
import './TopHubSignalPanel.css';

type Proposal = {
  id: string;
  title: string;
  impact: 'high' | 'medium' | 'low';
  impactLabel?: string;
  description: string;
  action: string;
  runbookUrl?: string;
};

type ProposalsPayload = {
  hubName: string;
  updatedAt: string;
  proposals: Proposal[];
};

// Fallback static data (matches Candidate 1)
const FALLBACK_HUB_DATA: Record<string, Proposal[]> = {
  MOC: [
    {
      id: 'moc-1',
      title: 'Right-size oversized dev instances',
      impact: 'high',
      impactLabel: 'Save $8k/mo',
      description: '30% of dev workloads run on larger-than-needed instance types.',
      action: 'Apply rightsizing policy to dev/staging accounts.',
    },
    {
      id: 'moc-2',
      title: 'Increase RI coverage for steady-state services',
      impact: 'high',
      impactLabel: 'Save $15k/mo',
      description: 'Current RI coverage ~45%; target 75% for predictable workloads.',
      action: 'Purchase 1-year convertible RIs for core services.',
    },
    {
      id: 'moc-3',
      title: 'Enable S3 Intelligent-Tiering for cold data',
      impact: 'medium',
      impactLabel: 'Save $3k/mo',
      description: 'Cold objects in standard tier driving avoidable storage cost.',
      action: 'Apply lifecycle rule to move >30d objects to Intelligent-Tiering.',
    },
  ],
};

const fetcher = (url: string) => fetch(url).then((r) => r.json());

const impactColor = {
  high: 'var(--impact-high, #ef4444)',
  medium: 'var(--impact-medium, #f59e0b)',
  low: 'var(--impact-low, #10b981)',
} as const;

export default function TopHubSignalPanel() {
  const hubName =
    (typeof process !== 'undefined' &&
      (process.env.VITE_HUB_NAME || process.env.NEXT_PUBLIC_HUB_NAME)) ||
    'MOC';

  const { data, error, isLoading } = useSWR<ProposalsPayload>(
    `/data/hubs/${hubName}/proposals.json`,
    fetcher,
    {
      revalidateOnFocus: false,
      refreshInterval: 5 * 60 * 1000, // 5 minutes
      shouldRetryOnError: false,
    }
  );

  const proposals = useMemo(() => {
    if (data?.proposals?.length) return data.proposals.slice(0, 3);
    return FALLBACK_HUB_DATA[hubName]?.slice(0, 3) || [];
  }, [data, hubName]);

  const isEmpty = !isLoading && proposals.length === 0;

  return (
    <section className="top-hub-panel" aria-label={`Top ${hubName} signals`}>
      <header className="top-hub-header">
        <h3 className="top-hub-title">Top Hub: {hubName}</h3>
        <span className="top-hub-badge">{proposals.length} signals</span>
      </header>

      {isLoading && (
        <div className="top-hub-loading" role="status" aria-label="Loading proposals">
          {Array.from({ length: 3 }).map((_, i) => (
           
