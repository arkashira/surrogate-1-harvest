# Costinel / quality

## Final Synthesis — Highest-Value Incremental Improvement (<2h)

**Ship a Top-Hub Signal Panel** on the Costinel dashboard that surfaces the most-connected hub (default **MOC**) and its top 3 actionable, cost-impact proposals using a **CDN-first, rate-limit-safe, zero-API-during-training, and audit-traceable** pattern.

This delivers immediate governance value (Sense + Signal) with zero schema changes, no infra work, and safe fallback behavior.

---

## Resolved Implementation Plan (≤2h)

1. **Add data file** `data/top-hub.json`  
   - Contains: `{ hub, proposals: [{ title, impact, savings, repo, path, cdnUrl, auditId, context }] }`  
   - Produced by an external orchestration job (or manually curated).  
   - Keeps runtime free of HF API calls and rate-limit risk.

2. **Create utility** `lib/topHubSignal.ts`  
   - Reads `data/top-hub.json` (or fallback to `{ hub: "MOC", proposals: [] }`).  
   - Exposes `getTopHubSignals()` with typed return.  
   - No client secrets; no runtime HF tokens.

3. **Add route** `app/api/top-hub/route.ts` (optional, for client hydration)  
   - GET endpoint returning the JSON above.  
   - `Cache-Control: s-maxage=300, stale-while-revalidate=60`.  
   - Server-side retry on 429: wait 360s before retry.

4. **Create panel** `components/TopHubSignalPanel.tsx`  
   - **Server Component by default** (reads `data/top-hub.json`).  
   - Optional client hydration via SWR on `/api/top-hub` if live updates are desired.  
   - Shows `Sense + Signal` badge and per-proposal audit trail link (`/audit/{auditId}`).  
   - CDN-safe links: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.

5. **Integrate into dashboard** `app/dashboard/page.tsx`  
   - Place `<TopHubSignalPanel />` near cost summary.

6. **Lightning/Studio reuse guard (if training triggered elsewhere)**  
   - Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse running ones.  
   - On idle stop, restart with `target.start(machine=Machine.L40S)`.

7. **Commit-cap mitigation (if pushing datasets)**  
   - Hash slug → pick sibling repo index `hash(slug) % 5` to spread writes (640/hr aggregate).

8. **Tests & lint**  
   - Type-check utility + route + component.  
   - Verify CDN URLs resolve without auth headers.

---

## Key Code Snippets

### `data/top-hub.json` (example)

```json
{
  "hub": "MOC",
  "proposals": [
    {
      "title": "CDN-first dataset access",
      "impact": "high",
      "savings": 1200,
      "repo": "axentx/knowledge-rag",
      "path": "moc/cdn-proposal-01.json",
      "cdnUrl": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/moc/cdn-proposal-01.json",
      "auditId": "AUD-2025-001",
      "context": "Bypass HF API during training; use CDN URLs only."
    }
  ]
}
```

---

### `lib/topHubSignal.ts`

```ts
import fs from "fs";
import path from "path";

export type Proposal = {
  title: string;
  impact: "high" | "medium" | "low";
  savings: number;
  repo: string;
  path: string;
  cdnUrl: string;
  auditId: string;
  context: string;
};

export type HubSignal = {
  hub: string;
  proposals: Proposal[];
};

const DATA_PATH = path.join(process.cwd(), "data", "top-hub.json");
const FALLBACK: HubSignal = { hub: "MOC", proposals: [] };

export function getTopHubSignals(): HubSignal {
  try {
    if (!fs.existsSync(DATA_PATH)) return FALLBACK;
    const raw = fs.readFileSync(DATA_PATH, "utf8");
    const parsed = JSON.parse(raw) as HubSignal;
    return parsed;
  } catch {
    return FALLBACK;
  }
}
```

---

### `app/api/top-hub/route.ts`

```ts
import { NextResponse } from "next/server";
import { getTopHubSignals } from "@/lib/topHubSignal";

export async function GET() {
  try {
    const data = getTopHubSignals();
    return NextResponse.json(data, {
      headers: { "Cache-Control": "s-maxage=300, stale-while-revalidate=60" },
    });
  } catch {
    return NextResponse.json(
      { error: "Unable to fetch top-hub signal" },
      { status: 500 }
    );
  }
}
```

---

### `components/TopHubSignalPanel.tsx`

```tsx
import { getTopHubSignals } from "@/lib/topHubSignal";

export default function TopHubSignalPanel() {
  const { hub, proposals } = getTopHubSignals();
  if (!proposals || proposals.length === 0) return null;

  return (
    <div className="rounded-lg border bg-card p-4">
      <h3 className="mb-3 text-sm font-semibold">
        Top-Hub Signal: <span className="text-primary">{hub}</span>
        <span className="ml-2 inline-flex items-center rounded bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
          Sense + Signal
        </span>
      </h3>
      <div className="grid gap-2 sm:grid-cols-3">
        {proposals.map((p, i) => (
          <a
            key={i}
            href={p.cdnUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="block rounded-md border p-3 hover:bg-accent"
          >
            <p className="text-sm font-medium">{p.title}</p>
            <p className="text-xs text-muted-foreground">
              {p.repo} — {p.path}
            </p>
            <p className="mt-1 text-xs">
              <span className="font-medium">Impact:</span> {p.impact}
              {p.savings > 0 && (
                <span className="ml-2">
                  <span className="font-medium">Est. savings:</span> ${p.savings}/mo
                </span>
              )}
            </p>
            <p className="mt-2 text-xs">
              <a
                href={`/audit/${p.auditId}`}
                className="underline underline-offset-2"
              >
                Audit: {p.auditId}
              </a>
            </p>
          </a>
        ))}
      </div>
    </div>
  );
}
```
