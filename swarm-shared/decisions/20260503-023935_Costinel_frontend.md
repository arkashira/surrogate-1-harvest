# Costinel / frontend

## Highest-Value Incremental Improvement (<2h)

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals — **zero Hugging Face API calls during render**, fully CDN-backed, and ready for human review.

---

## Implementation Plan (≤2h)

1. **Data source** (5m)  
   - Use pre-generated JSON from knowledge-rag export:  
     `public/data/top-hub-signals.json`  
   - Schema: `{ hub, description, signals: [{ title, impact, proposal, cdnPath }] }`

2. **Component** (45m)  
   - Create `src/components/TopHubSignalPanel.tsx`  
   - Fetch JSON at build time (Next.js `getStaticProps`) or client-side `fetch` from `/data/...` (CDN).  
   - Render hub card + 3 signal cards with impact badges and “Review Proposal” action.

3. **Route integration** (20m)  
   - Mount panel on `/dashboard` above main cost analytics grid.  
   - Mobile-first responsive layout (grid → stack).

4. **Styling** (20m)  
   - Use existing design tokens (colors, spacing).  
   - Impact badges: high/medium/low color scale.

5. **CDN-first guarantee** (10m)  
   - Confirm no HF API usage in component or data loader.  
   - Add comment header: `// CDN-only; zero HF API during render`.

6. **Testing & ship** (20m)  
   - Smoke test locally, verify JSON loads and panel renders.  
   - Build and check static export (if applicable).  
   - Commit with changelog note.

---

## Code Snippets

### public/data/top-hub-signals.json
```json
{
  "hub": "MOC",
  "description": "Mission Operations Center — highest connectivity across cost governance workflows",
  "signals": [
    {
      "title": "Idle Reserved Instance Coverage Gap",
      "impact": "high",
      "proposal": "Shift 30% of on-demand RIs to convertible RIs and enable instance scheduler for dev/test",
      "cdnPath": "/batches/mirror-merged/2026-05-03/moc-ri-coverage.parquet"
    },
    {
      "title": "Cross-Region Data Transfer Spike",
      "impact": "medium",
      "proposal": "Enable VPC endpoints for S3 in us-east-1 and enable CloudFront caching for egress",
      "cdnPath": "/batches/mirror-merged/2026-05-03/moc-xfer-spike.parquet"
    },
    {
      "title": "Underutilized GPU Nodes in ML Workspace",
      "impact": "medium",
      "proposal": "Downsize to L40S on Lightning public tier and enable auto-stop after 30m idle",
      "cdnPath": "/batches/mirror-merged/2026-05-03/moc-gpu-util.parquet"
    }
  ]
}
```

### src/components/TopHubSignalPanel.tsx
```tsx
'use client';

import { useEffect, useState } from 'react';
import styles from './TopHubSignalPanel.module.css';

interface Signal {
  title: string;
  impact: 'high' | 'medium' | 'low';
  proposal: string;
  cdnPath: string;
}

interface HubData {
  hub: string;
  description: string;
  signals: Signal[];
}

const impactColor = {
  high: 'var(--impact-high, #ef4444)',
  medium: 'var(--impact-medium, #f59e0b)',
  low: 'var(--impact-low, #10b981)',
};

export default function TopHubSignalPanel() {
  const [data, setData] = useState<HubData | null>(null);

  useEffect(() => {
    // CDN-only fetch; zero HF API during render
    fetch('/data/top-hub-signals.json')
      .then((res) => res.json())
      .then(setData)
      .catch((err) => console.error('Failed to load top-hub signals', err));
  }, []);

  if (!data) return <div className={styles.loading}>Loading signals…</div>;

  return (
    <section className={styles.panel} aria-labelledby="hub-title">
      <div className={styles.header}>
        <h2 id="hub-title" className={styles.hubName}>{data.hub}</h2>
        <p className={styles.hubDesc}>{data.description}</p>
      </div>

      <div className={styles.signals}>
        {data.signals.map((s, i) => (
          <article key={i} className={styles.signalCard}>
            <div className={styles.signalHeader}>
              <h3 className={styles.signalTitle}>{s.title}</h3>
              <span
                className={styles.impactBadge}
                style={{ backgroundColor: impactColor[s.impact] }}
              >
                {s.impact}
              </span>
            </div>
            <p className={styles.proposal}>{s.proposal}</p>
            <div className={styles.actions}>
              <a
                href={s.cdnPath}
                className={styles.link}
                target="_blank"
                rel="noopener noreferrer"
              >
                View details
              </a>
              <button className={styles.reviewBtn} type="button">
                Review Proposal
              </button>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
```

### src/components/TopHubSignalPanel.module.css
```css
.panel {
  background: var(--bg-card, #ffffff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 12px;
  padding: 20px;
  margin-bottom: 24px;
}

.header {
  margin-bottom: 16px;
}

.hubName {
  font-size: 20px;
  font-weight: 700;
  margin: 0 0 4px;
  color: var(--text-primary, #111827);
}

.hubDesc {
  margin: 0;
  color: var(--text-secondary, #6b7280);
  font-size: 14px;
}

.signals {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
}

.signalCard {
  background: var(--bg-elevated, #f9fafb);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.signalHeader {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}

.signalTitle {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
  color: var(--text-primary, #111827);
  flex: 1;
}

.impactBadge {
  font-size: 11px;
  font-weight: 700;
  color: #fff;
  padding: 2px 8px;
  border-radius: 999px;
  text-transform: uppercase;
  flex-shrink: 0;
}

.proposal {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary, #4b5563);
  line-height: 1.5;
}

.actions {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}

.link {
  font-size: 13px;
  color: var(--accent, #3b82f6);
  text-decoration: none;
}

.link:hover {
  text-decoration:
