# Costinel / discovery

Candidate 3:
## Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Timebox**: <2h (frontend-only, zero backend changes).  
**Assumptions**:  
- A JSON endpoint or static file at `/data/knowledge-rag/top-hubs.json` (or similar) provides `{ hub, score, signals: [{ title, summary, href }] }`.  
- If endpoint does not exist yet, we’ll create a static JSON file in `public/data/` and consume it (avoids backend work).  
- Uses existing design tokens/components where possible.

---

### Steps (ordered by priority)

1. **Create static data file** (`public/data/top-hub.json`) with shape:
   ```json
   {
     "hub": "MOC",
     "score": 94,
     "signals": [
       {
         "title": "Cost anomaly spike in us-east-1",
         "summary": "Detected 2.4× baseline spend on EBS snapshots; review unattached volumes.",
         "href": "/insights/anomalies/2026-05-02/ebs-us-east-1"
       },
       {
         "title": "RI coverage below target",
         "summary": "Compute savings opportunity: 38% RI coverage vs 70% goal for m5 family.",
         "href": "/recommendations/ri/compute-m5"
       },
       {
         "title": "Tag compliance drift",
         "summary": "12% of new resources missing CostCenter tag; auto-remediation disabled (Sense+Signal only).",
         "href": "/governance/tags/compliance"
       }
     ]
   }
   ```

2. **Add API helper** (`src/api/knowledgeRag.js`) — lightweight fetch with graceful fallback:
   ```js
   const ENDPOINT = '/data/knowledge-rag/top-hubs.json';
   const FALLBACK = '/data/top-hub.json';

   export async function fetchTopHub({ limit = 3 } = {}) {
     try {
       const r = await fetch(ENDPOINT, { credentials: 'same-origin' });
       if (!r.ok) throw new Error('No primary');
       const d = await r.json();
       return { hub: d.hub, score: d.score, signals: d.signals.slice(0, limit) };
     } catch {
       const r = await fetch(FALLBACK);
       const d = await r.json();
       return { hub: d.hub, score: d.score, signals: d.signals.slice(0, limit) };
     }
   }
   ```

3. **Create component** (`src/components/costinel/TopHubSignalCard.vue`) — uses existing tokens and emits no actions:
   ```vue
   <template>
     <section class="hub-card">
       <header class="hdr">
         <h3 class="t">Top Hub Signal</h3>
         <span class="sub">Knowledge-RAG • Most-connected</span>
       </header>

       <div v-if="loading" class="state">Loading…</div>

       <div v-else-if="error" class="state warn">Using static fallback</div>

       <div v-else class="body">
         <div class="hub-row">
           <strong class="hub-name">{{ hub }}</strong>
           <span class="hub-score">{{ score }}</span>
         </div>

         <ul class="list">
           <li v-for="s in signals" :key="s.title" class="item">
             <a :href="s.href" target="_blank" rel="noopener" class="link">
               <span class="item-title">{{ s.title }}</span>
               <p class="item-desc">{{ s.summary }}</p>
             </a>
           </li>
         </ul>
       </div>

       <footer class="ft">Sense + Signal — no execution</footer>
     </section>
   </template>

   <script>
   import { fetchTopHub } from '@/api/knowledgeRag';

   export default {
     name: 'TopHubSignalCard',
     data() {
       return { hub: 'MOC', score: 0, signals: [], loading: true, error: false };
     },
     async mounted() {
       try {
         const d = await fetchTopHub({ limit: 3 });
         this.hub = d.hub;
         this.score = d.score;
         this.signals = d.signals;
       } catch {
         this.error = true;
       } finally {
         this.loading = false;
       }
     }
   };
   </script>

   <style scoped>
   .hub-card { border: 1px solid var(--border, #e6e9ef); border-radius: 10px; padding: 16px; background: #fff; max-width: 380px; }
   .hdr { margin-bottom: 8px; }
   .t { margin: 0; font-size: 16px; }
   .sub { color: #6b7280; font-size: 12px; }
   .state { color: #6b7280; font-size: 13px; padding: 8px 0; }
   .warn { color: #b91c1c; }
   .hub-row { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
   .hub-name { font-size: 15px; }
   .hub-score { font-size: 13px; color: #6b7280; }
   .list { list-style:none; padding:0; margin:0; }
   .item { margin-bottom: 8px; }
   .link { display:block; padding:8px; border-radius:6px; text-decoration:none; color:inherit; transition:background .12s; }
   .link:hover { background:#f8fafc; }
   .item-title { font-weight:600; font-size:13px; color:#111827; display:block; margin-bottom:4px; }
   .item-desc { margin:0; font-size:12px; color:#4b5563; }
   .ft { margin-top:10px; font-size:11px; color:#9ca3af; text-align:right; }
   </style>
   ```

4. **Mount in dashboard** (`src/views/Dashboard.vue`) — place in sidebar/top-row:
   ```vue
   <template>
     <div class="dashboard">
       <aside class="sidebar">
         <TopHubSignalCard />
         <!-- other widgets -->
       </aside>
       <!-- main content -->
     </div>
   </template>

   <script>
   import TopHubSignalCard from '@/components/costinel/TopHubSignalCard.vue';

   export default {
     components: { TopHubSignalCard }
   };
   </script>
   ```

5. **Verify** — run dev server, confirm card shows hub + 3 signals, links open in new tab, no console errors, and fallback works when primary file is missing.

---

Candidate 4:
## Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Timebox**: <2h (frontend-only, zero backend changes).  
**Assumptions**:  
- A JSON endpoint or static file at `/data/knowledge-rag/top-hubs.json` (or similar) provides `{ hub, score, signals: [{ title, summary, href }] }`.  
- If endpoint does not exist yet, we’ll create a static JSON file in `public/data/` and consume it (avoids backend work).  
- Uses existing design tokens/components where possible.

---

### Steps (ordered by priority)

1. **Create static data file** (`public/data/top-hub.json`) with shape:
   ```json
   {
     "hub": "MOC",
     "score": 94,
     "signals": [
       {
         "title": "Cost anomaly spike in us-east-1",
         "summary": "Detected 2.4
