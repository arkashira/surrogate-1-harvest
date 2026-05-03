# Costinel / quality

## Implementation Plan — Costinel Quality: Top-Hub Signal Card (Read-Only)

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution, no runtime mutations)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, concise context, and provenance — embedded in the Costinel dashboard as a signal-only widget.

---

### 1) High-value incremental improvement
Add a **Top-Hub Signal Card** to the dashboard that:
- Shows the most-connected hub (from local knowledge graph / last-known `knowledge-rag` export)
- Displays hub score, short insight, and timestamp
- Links to related docs (read-only)
- Zero backend writes; entirely client-side render from static JSON
- Uses existing design tokens and respects “Sense + Signal” philosophy

---

### 2) Concrete implementation steps (≤2h)

1. **Create static signal payload** (if not present)  
   Path: `/opt/axentx/Costinel/data/top-hub-signal.json`  
   Content example:
   ```json
   {
     "hub": "MOC",
     "score": 0.94,
     "insight": "Most-connected node across cost governance policies; central to anomaly propagation and recommendation routing.",
     "relatedDocs": [
       { "title": "Cost Governance Playbook — MOC", "href": "/docs/playbook-moc.pdf" },
       { "title": "Anomaly Taxonomy", "href": "/docs/taxonomy.md" }
     ],
     "lastUpdated": "2026-05-02T23:59:00Z",
     "source": "knowledge-rag#graph"
   }
   ```

2. **Add read-only API route** (Next.js app assumed)  
   File: `/opt/axentx/Costinel/src/app/api/signals/top-hub/route.ts`
   ```ts
   import { NextResponse } from 'next/server';
   import fs from 'fs';
   import path from 'path';

   export async function GET() {
     try {
       const filePath = path.join(process.cwd(), 'data', 'top-hub-signal.json');
       const raw = fs.readFileSync(filePath, 'utf8');
       const payload = JSON.parse(raw);
       // Strictly read-only; no mutations, no writes
       return NextResponse.json(payload, {
         headers: {
           'Cache-Control': 'public, max-age=300, stale-while-revalidate=60',
         },
       });
     } catch (err) {
       return NextResponse.json(
         { error: 'Signal unavailable' },
         { status: 503 }
       );
     }
   }
   ```

3. **Create TopHubSignalCard component**  
   File: `/opt/axentx/Costinel/src/components/TopHubSignalCard.tsx`
   ```tsx
   'use client';

   import { useEffect, useState } from 'react';

   interface RelatedDoc {
     title: string;
     href: string;
   }

   interface TopHubSignal {
     hub: string;
     score: number;
     insight: string;
     relatedDocs: RelatedDoc[];
     lastUpdated: string;
     source: string;
   }

   export default function TopHubSignalCard() {
     const [signal, setSignal] = useState<TopHubSignal | null>(null);
     const [loading, setLoading] = useState(true);

     useEffect(() => {
       fetch('/api/signals/top-hub')
         .then((res) => res.json())
         .then((data) => {
           setSignal(data);
           setLoading(false);
         })
         .catch(() => setLoading(false));
     }, []);

     if (loading) {
       return (
         <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
           <p className="text-sm text-gray-500">Loading signal…</p>
         </div>
       );
     }

     if (!signal) {
       return (
         <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
           <p className="text-sm text-gray-500">Signal unavailable</p>
         </div>
       );
     }

     return (
       <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
         <div className="flex items-start justify-between">
           <div>
             <h3 className="text-sm font-medium text-gray-900">Top-Hub Signal</h3>
             <p className="mt-1 text-2xl font-semibold text-blue-600">{signal.hub}</p>
             <p className="mt-1 text-xs text-gray-500">
               Score: {(signal.score * 100).toFixed(0)}/100 &bull;{' '}
               {new Date(signal.lastUpdated).toLocaleDateString()}
             </p>
           </div>
           <span className="inline-flex items-center rounded bg-blue-50 px-2 py-1 text-xs font-medium text-blue-700">
             {signal.source}
           </span>
         </div>

         <p className="mt-3 text-sm text-gray-700">{signal.insight}</p>

         {signal.relatedDocs.length > 0 && (
           <ul className="mt-4 space-y-1">
             {signal.relatedDocs.map((doc, idx) => (
               <li key={idx}>
                 <a
                   href={doc.href}
                   target="_blank"
                   rel="noopener noreferrer"
                   className="text-xs text-blue-600 hover:underline"
                 >
                   {doc.title}
                 </a>
               </li>
             ))}
           </ul>
         )}

         <p className="mt-4 text-xs text-gray-400">
           Sense + Signal — ไม่ Execute
         </p>
       </div>
     );
   }
   ```

4. **Embed card in dashboard**  
   File: `/opt/axentx/Costinel/src/app/dashboard/page.tsx` (or equivalent)
   ```tsx
   import TopHubSignalCard from '@/components/TopHubSignalCard';

   export default function DashboardPage() {
     return (
       <main className="p-6">
         <div className="mb-6">
           <h1 className="text-2xl font-bold text-gray-900">Cost Governance Dashboard</h1>
           <p className="text-sm text-gray-600">Sense + Signal — ไม่ Execute</p>
         </div>

         <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
           <TopHubSignalCard />
           {/* existing cards */}
         </div>
       </main>
     );
   }
   ```

5. **Optional: cron-safe update script** (run from orchestration host, not in-app)  
   File: `/opt/axentx/Costinel/scripts/update-top-hub-signal.sh`
   ```bash
   #!/usr/bin/env bash
   # Updates top-hub-signal.json from knowledge-rag export (read-only operation).
   set -euo pipefail
   export SHELL=/bin/bash

   DEST="/opt/axentx/Costinel/data/top-hub-signal.json"
   TMP=$(mktemp)

   # Example: pull latest hub from knowledge-rag (replace with actual command)
   # knowledge-rag --query "top hub" --format json > "$TMP"
   # For now, simulate safe update:
   cat > "$TMP" <<'EOF'
   {
     "hub": "MOC",
     "score": 0.94,
     "insight": "Most-connected node across cost governance policies; central to anomaly propagation and recommendation routing.",
     "relatedDocs": [
       { "title": "Cost Governance Playbook — MOC", "href": "/docs/playbook-moc.pdf" },
       { "title": "Anomaly Taxonomy", "href": "/docs/taxonomy.md" }
     ],
     "lastUpdated": "2026-05-02T23:59:00Z",
     "source
