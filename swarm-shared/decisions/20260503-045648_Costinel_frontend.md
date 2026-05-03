# Costinel / frontend

## Implementation Plan — Top Hub Signal Panel (CDend-first, frontend-only)

**Scope**: Add a lightweight, resilient Top Hub signal card to the Costinel dashboard.  
**Effort**: ~60–90 minutes (frontend only).  
**Mechanism**: CDN JSON fetch (no auth, no backend) with local fallback and graceful degradation. Fails open (never blocks dashboard).

### Why this is the highest-value incremental improvement
- Directly applies the **top-hub doc insight** pattern (review most-connected hub before planning).
- Uses the **HF CDN bypass** pattern to avoid API rate limits and keep frontend-only.
- Adds immediate business value by surfacing the most-connected hub (“MOC”) as a signal card without backend changes or auth.
- Fits <2h with zero infra/ops work.

---

### Implementation Steps

1. **Create CDN JSON asset**  
   Place a small JSON file at a public CDN path (e.g., `datasets/axentx/costinel-signals/resolve/main/top-hub.json`).  
   Content (example):
   ```json
   {
     "hub": "MOC",
     "title": "Most-Connected Hub",
     "score": 94,
     "trend": "up",
     "updated": "2026-05-03T04:56:00Z",
     "summary": "MOC remains the top hub by cross-links and signal volume. Prioritize governance signals that route through MOC for maximum reach.",
     "actions": [
       { "label": "View signals", "href": "/signals?hub=MOC" },
       { "label": "Review hub", "href": "/hubs/MOC" }
     ]
   }
   ```

2. **Add TopHubSignalCard component** (`src/components/TopHubSignalCard.jsx`)  
   - CDN-first fetch with timeout + AbortController.
   - Local fallback to embedded JSON if CDN fails.
   - Skeleton loader while fetching.
   - Never throws; logs and renders fallback.

   ```jsx
   // src/components/TopHubSignalCard.jsx
   import React, { useEffect, useState } from 'react';

   const CDN_URL = 'https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub.json';

   const LOCAL_FALLBACK = {
     hub: 'MOC',
     title: 'Most-Connected Hub',
     score: 94,
     trend: 'up',
     updated: '2026-05-03T04:56:00Z',
     summary: 'MOC remains the top hub by cross-links and signal volume. Prioritize governance signals that route through MOC for maximum reach.',
     actions: [
       { label: 'View signals', href: '/signals?hub=MOC' },
       { label: 'Review hub', href: '/hubs/MOC' }
     ]
   };

   const TREND_ICONS = {
     up: '↑',
     down: '↓',
     stable: '→'
   };

   export default function TopHubSignalCard() {
     const [data, setData] = useState(null);
     const [loading, setLoading] = useState(true);

     useEffect(() => {
       const controller = new AbortController();
       const timeoutId = setTimeout(() => controller.abort(), 4000);

       fetch(CDN_URL, { signal: controller.signal, cache: 'no-cache' })
         .then(async (res) => {
           if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
           return res.json();
         })
         .then((json) => {
           setData(json);
           setLoading(false);
         })
         .catch((err) => {
           console.warn('[TopHubSignalCard] CDN fetch failed, using fallback:', err.message);
           setData(LOCAL_FALLBACK);
           setLoading(false);
         })
         .finally(() => clearTimeout(timeoutId));

       return () => controller.abort();
     }, []);

     if (loading) {
       return (
         <div className="top-hub-card card">
           <div className="skeleton" style={{ height: 20, width: '60%', marginBottom: 8 }} />
           <div className="skeleton" style={{ height: 14, width: '90%', marginBottom: 4 }} />
           <div className="skeleton" style={{ height: 14, width: '70%', marginBottom: 12 }} />
           <div className="skeleton" style={{ height: 32, width: '45%' }} />
         </div>
       );
     }

     if (!data) return null;

     return (
       <div className="top-hub-card card" role="region" aria-label={`Top hub: ${data.hub}`}>
         <div className="card-header">
           <h3 className="card-title">{data.title}</h3>
           <span className="hub-badge" title={`Score ${data.score}`}>
             {data.hub} {TREND_ICONS[data.trend] || '→'}
           </span>
         </div>

         <p className="card-summary">{data.summary}</p>

         <div className="card-meta">
           <small>Updated {new Date(data.updated).toLocaleString()}</small>
         </div>

         <div className="card-actions">
           {data.actions?.map((a, i) => (
             <a key={i} className="btn btn-ghost" href={a.href}>
               {a.label}
             </a>
           ))}
         </div>
       </div>
     );
   }
   ```

3. **Add minimal CSS** (`src/components/TopHubSignalCard.css`)  
   Keep styles scoped/light so it blends with existing dashboard.

   ```css
   /* src/components/TopHubSignalCard.css */
   .top-hub-card {
     border-left: 3px solid #3b82f6;
     padding: 1rem;
   }

   .card-header {
     display: flex;
     justify-content: space-between;
     align-items: flex-start;
     gap: 1rem;
     margin-bottom: 0.5rem;
   }

   .card-title {
     font-size: 0.875rem;
     font-weight: 600;
     margin: 0;
     color: #111827;
   }

   .hub-badge {
     font-size: 0.75rem;
     font-weight: 700;
     padding: 0.125rem 0.5rem;
     border-radius: 999px;
     background: #eff6ff;
     color: #2563eb;
     white-space: nowrap;
   }

   .card-summary {
     font-size: 0.8125rem;
     color: #4b5563;
     margin: 0 0 0.75rem 0;
     line-height: 1.4;
   }

   .card-meta {
     margin-bottom: 0.75rem;
   }

   .card-meta small {
     color: #9ca3af;
   }

   .card-actions {
     display: flex;
     gap: 0.5rem;
     flex-wrap: wrap;
   }

   .btn {
     font-size: 0.75rem;
     padding: 0.25rem 0.5rem;
     border-radius: 0.375rem;
     text-decoration: none;
     transition: background 0.15s;
   }

   .btn-ghost {
     background: #f3f4f6;
     color: #374151;
   }

   .btn-ghost:hover {
     background: #e5e7eb;
   }

   .skeleton {
     background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
     background-size: 200% 100%;
     animation: loading 1.5s infinite;
     border-radius: 
