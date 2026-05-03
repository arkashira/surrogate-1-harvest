# Costinel / backend

```tsx
          <p className="text-2xl font-bold tracking-tight text-primary">{hub}</p>
        </div>
        {updatedAt && (
          <span className="text-xs text-muted-foreground" title={updatedAt}>
            Updated {new Date(updatedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          </span>
        )}
      </div>

      <p className="mt-2 text-sm text-muted-foreground">{insight}</p>

      {related.length > 0 && (
        <ul className="mt-3 space-y-1" aria-label="Related docs">
          {related.map((doc, i) => (
            <li key={i}>
              <a
                href={doc.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs text-primary hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <ExternalLink className="h-3 w-3" aria-hidden="true" />
                {doc.title}
              </a>
            </li>
          ))}
        </ul>
      )}

      {!loading && !related.length && (
        <p className="mt-3 text-xs text-muted-foreground/70">No related documents available.</p>
      )}
    </div>
  );
}
```

#### 4) Dashboard integration (`src/pages/Dashboard.tsx`)
```tsx
// src/pages/Dashboard.tsx
import { TopHubSignalPanel } from '../components/TopHubSignalPanel';

export default function Dashboard() {
  return (
    <main className="flex flex-col gap-6 p-6">
      <header>
        <h1 className="text-2xl font-bold tracking-tight">Costinel</h1>
      </header>

      {/* Top signals row */}
      <section aria-label="Key signals" className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        <TopHubSignalPanel />
        {/* Reserve slots for additional signal panels */}
      </section>

      {/* Rest of dashboard content */}
    </main>
  );
}
```

---

### Why this merged version is correct + actionable
- **CDN-first, zero runtime HF API**: Uses a public CDN path with cache/no-auth and a strict JSON contract.  
- **Resilience**: Timeout + retry + graceful fallback to a safe default keeps UI functional when CDN is slow/missing.  
- **Sense + Signal (ไม่ Execute)**: Read-only fetch; no mutations, no backend, no DB.  
- **Polling**: Lightweight background refresh (default 5m) keeps signal current without user action.  
- **Accessibility & UX**: Semantic markup, keyboard-friendly links, loading skeleton, last-updated hint, and compact card layout fit existing dashboard.  
- **Fast to ship (<2h)**: Only frontend files; no infra/backend changes. Contract with ops is explicit (`top-hub.json` shape + CDN path).
