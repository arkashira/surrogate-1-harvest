# Costinel / discovery

Based on the provided AI proposals, I will synthesize the best parts and combine the strongest insights into a single, final answer. 

The goal is to create a read-only frontend card that surfaces the most-connected hub (e.g., "MOC") with contextual insights from knowledge-rag as a static, server-rendered card. 

Here's a comprehensive implementation plan:

**Scope**: Read-only frontend card (≤2h)

**Principle**: "Sense + Signal — ไม่ Execute" (strictly no writes, no runtime mutations, no self-execution)

**Goal**: Surface the most-connected hub with contextual insights from knowledge-rag as a static, observable signal for human review.

**Architecture**:

1. **Data source**: Pre-computed `top-hub.json` produced by an offline `knowledge-rag` job.
2. **Delivery**: Server reads file → injects into SSR props → React renders card.
3. **No runtime writes**: Card is purely presentational; no POST/PUT/DELETE, no background jobs, no cron inside component.
4. **No client mutations**: No `useEffect` writes to localStorage/indexedDB that affect runtime state.

**Concrete Implementation Steps**:

1. **Create read-only API route** (Next.js route handler): `GET /api/signals/top-hub`
	* Returns `{ hub: { id, label, degree, summary }, contexts: [{ title, url, score, snippet }], generatedAt }`
	* No writes; no background jobs; cache-control `public, max-age=300` (5m) to avoid hammering upstream.
2. **Add frontend card component**: `components/TopHubSignalCard.tsx`
	* Fetches `/api/signals/top-hub` client-side (or via server component) and renders card.
	* Skeleton + error states included.
3. **Wire into dashboard layout**: Place card in the primary dashboard grid (top-row or sidebar depending on layout).
	* Ensure mobile responsive.
4. **Add read-only graph route link**: `/graph/hubs/[id]` (read-only page) to enable "View in Graph" navigation.
5. **Tests & lint**: Quick smoke test for API shape and component render.

**Code Snippets**:

* `app/dashboard/top-hub-card.tsx` (server-side loader and card component)
* `app/api/signals/top-hub/route.ts` (read-only API route)
* `data/top-hub.json` (pre-computed payload example)

**Validation Checklist**:

* `data/top-hub.json` exists and is valid JSON.
* Card renders without client-side JS required (SSR).
* No `fetch`/`POST`/`PUT`/`DELETE` in component or API routes for this feature.
* No background jobs or `setInterval` in component.
* Links open in new tab (`noopener noreferrer`) and are strictly read-only.

**Estimated Effort**:

* Scaffold + component: ~45m
* Integrate into dashboard + styling: ~30m
* Validate offline job contract + tests: ~30m
* Buffer: ~15m
**Total**: ~2h (fits scope)

By following this implementation plan, you can create a read-only frontend card that effectively surfaces the most-connected hub with contextual insights from knowledge-rag, while adhering to the principles of "Sense + Signal — ไม่ Execute" and minimizing runtime mutations.
