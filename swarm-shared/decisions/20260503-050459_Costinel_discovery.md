# Costinel / discovery

## Implementation Plan — Top Hub Signal Panel (CDN-first, frontend-only)

**Scope**: Add a lightweight, resilient Top Hub signal card to the Costinel dashboard that surfaces the most-connected hub (e.g., "MOC") with contextual insights.  
**Effort**: ~60–90 minutes (frontend only).

### Why this is highest-value (<2h)
- Directly applies the **top-hub doc insight** pattern (review most-connected hub before planning).
- Uses **CDN-first** strategy to avoid HF API rate limits (no backend changes, zero ingestion cost).
- Delivers immediate governance context to Costinel users without touching execution paths (fits Sense + Signal philosophy).
- Frontend-only keeps scope tight and shippable in one session.

---

### Implementation Steps

1. **Create a static hub index file**  
   Generate `public/data/hubs/top-hub.json` containing:
   - `hubId`, `hubName`, `hubSlug`
   - `connectionsCount`
   - `insights` (short list of contextual bullets)
   - `lastUpdatedISO`

   Example:
   ```json
   {
     "hubId": "MOC",
     "hubName": "Mission Operations Center",
     "hubSlug": "mission-operations-center",
     "connectionsCount": 127,
     "insights": [
       "Most-connected hub across cost, compliance, and change signals.",
       "Primary consumer of anomaly alerts from AWS and GCP.",
       "Recommended to review RI coverage proposals before next procurement window."
     ],
     "lastUpdatedISO": "2026-05-03T05:04:02Z"
   }
   ```

2. **Add a TopHubCard component**  
   Location: `src/components/TopHubCard.tsx` (or `.tsx` equivalent in your stack).  
   Responsibilities:
   - Fetch `public/data/hubs/top-hub.json` via CDN (no auth, no API).
   - Graceful fallback if fetch fails (show placeholder with cached defaults).
   - Render a concise card with hub name, connection count, and insights list.

   Code snippet:
   ```tsx
   import { useEffect, useState } from 'react';

   interface HubData {
     hubId: string;
     hubName: string;
     hubSlug: string;
     connectionsCount: number;
     insights: string[];
     lastUpdatedISO: string;
   }

   const DEFAULT_HUB: HubData = {
     hubId: 'MOC',
     hubName: 'Mission Operations Center',
     hubSlug: 'mission-operations-center',
     connectionsCount: 0,
     insights: ['Loading hub insights...'],
     lastUpdatedISO: new Date().toISOString(),
   };

   export default function TopHubCard() {
     const [hub, setHub] = useState<HubData>(DEFAULT_HUB);
     const [loading, setLoading] = useState(true);

     useEffect(() => {
       fetch('/data/hubs/top-hub.json', { cache: 'no-cache' })
         .then((res) => {
           if (!res.ok) throw new Error('Failed to fetch top hub');
           return res.json();
         })
         .then((data) => {
           setHub(data);
           setLoading(false);
         })
         .catch(() => {
           setHub(DEFAULT_HUB);
           setLoading(false);
         });
     }, []);

     return (
       <div className="rounded-lg border bg-card p-4 shadow-sm">
         <div className="flex items-center justify-between">
           <div>
             <h3 className="text-sm font-medium text-muted-foreground">Top Hub</h3>
             <p className="text-lg font-semibold">{hub.hubName}</p>
             <p className="text-xs text-muted-foreground">
               {hub.connectionsCount > 0
                 ? `${hub.connectionsCount.toLocaleString()} connections`
                 : 'No connection data'}
             </p>
           </div>
           {!loading && (
             <span className="inline-flex items-center rounded bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
               {hub.hubId}
             </span>
           )}
         </div>

         {hub.insights.length > 0 && (
           <ul className="mt-3 space-y-1">
             {hub.insights.map((insight, i) => (
               <li key={i} className="text-xs text-muted-foreground">
                 • {insight}
               </li>
             ))}
           </ul>
         )}

         <p className="mt-2 text-xs text-muted-foreground/60">
           Updated {new Date(hub.lastUpdatedISO).toLocaleDateString()}
         </p>
       </div>
     );
   }
   ```

3. **Mount the card on the dashboard**  
   Place `TopHubCard` near the top of the main dashboard view (e.g., under the summary KPIs or in a sidebar panel).  
   Example placement in `src/pages/Dashboard.tsx`:
   ```tsx
   import TopHubCard from '@/components/TopHubCard';

   export default function Dashboard() {
     return (
       <div className="grid gap-6">
         {/* Existing KPIs */}
         <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">{/* ... */}</div>

         {/* Top Hub Signal */}
         <div className="md:max-w-lg">
           <TopHubCard />
         </div>

         {/* Rest of dashboard */}
         {/* ... */}
       </div>
     );
   }
   ```

4. **Styling & responsiveness**  
   - Use existing design tokens (colors, spacing, typography) to match Costinel’s UI.
   - Ensure card collapses gracefully on mobile (full width, readable text).

5. **CDN update workflow (ops note)**  
   - To update the top hub, regenerate `public/data/hubs/top-hub.json` and commit.  
   - Because it’s served from `public/`, it’s CDN-cached; add a cache-busting query param or short cache TTL during updates if needed (e.g., `?v=20260503`).

---

### Acceptance Criteria
- Card appears on dashboard and shows hub name, ID, connection count, and insights.
- If CDN fetch fails, falls back to defaults without breaking the page.
- No backend changes or API keys required.
- Follows Sense + Signal principle (informational only, no execution).

---

### Tags
#knowledge-rag #graph #hub #frontend #cdn #costinel
