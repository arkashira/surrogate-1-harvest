# Full-Stack Development Skills & Patterns

> Ultimate reference loaded into every AI coding session. Practical patterns with code examples.
> For senior engineers who want a reference, not a tutorial.

---

# 1. Code Architecture & Design

## Architecture Styles — Decision Matrix

| Style | When | Avoid When |
|---|---|---|
| **Modular Monolith** | Starting out, small team, unclear boundaries | Team >30, independent deploy needed |
| **Microservices** | Clear domain boundaries, independent scaling/deploy | Small team, unclear boundaries, premature |
| **Vertical Slice** | CRUD-heavy apps, feature teams | Deep shared domain logic |
| **Hexagonal (Ports & Adapters)** | Complex domain, many integrations | Simple CRUD apps |
| **CQRS** | Read/write patterns differ significantly | Simple CRUD |
| **Event Sourcing** | Audit trail critical, temporal queries needed | Simple state, team inexperienced |

## Clean / Hexagonal / Onion Architecture

```
┌─────────────────────────────────────────┐
│  Infrastructure (DB, HTTP, Queue, FS)   │  ← Adapters (implements ports)
├─────────────────────────────────────────┤
│  Application (Use Cases, Commands)      │  ← Orchestrates domain
├─────────────────────────────────────────┤
│  Domain (Entities, Value Objects, Rules)│  ← Pure logic, ZERO dependencies
└─────────────────────────────────────────┘
```

**Rule:** Dependencies point inward. Domain never imports from infrastructure.

```typescript
// Port (interface in domain)
interface OrderRepository {
  save(order: Order): Promise<void>;
  findById(id: OrderId): Promise<Order | null>;
}

// Adapter (infrastructure implements port)
class PostgresOrderRepository implements OrderRepository {
  async save(order: Order): Promise<void> {
    await this.db.query(`INSERT INTO orders ...`, [order.id, order.total]);
  }
}

// Use case (application layer orchestrates)
class PlaceOrderUseCase {
  constructor(
    private orders: OrderRepository,     // injected port
    private payments: PaymentGateway,    // injected port
    private events: EventPublisher,      // injected port
  ) {}
  
  async execute(cmd: PlaceOrderCommand): Promise<Result<Order, OrderError>> {
    const order = Order.create(cmd);     // domain logic
    const charge = await this.payments.charge(order.total);
    if (!charge.ok) return Err(new PaymentFailed(charge.error));
    await this.orders.save(order);
    await this.events.publish(new OrderPlaced(order));
    return Ok(order);
  }
}
```

## Domain-Driven Design (DDD)

### Building Blocks

| Concept | What | Example |
|---|---|---|
| **Entity** | Identity-based, mutable | `User`, `Order` |
| **Value Object** | Equality by value, immutable | `Email`, `Money`, `Address` |
| **Aggregate** | Consistency boundary, one root entity | `Order` (root) + `OrderLine` (child) |
| **Domain Event** | Something that happened | `OrderPlaced`, `PaymentFailed` |
| **Repository** | Persistence abstraction per aggregate | `OrderRepository` |
| **Domain Service** | Logic spanning multiple aggregates | `TransferService` |
| **Application Service** | Use case orchestration | `PlaceOrderUseCase` |

### Aggregate Rules
1. Reference other aggregates by ID only, never direct object reference
2. One transaction = one aggregate. Cross-aggregate consistency via domain events
3. Keep aggregates small. Large aggregates = contention + performance problems
4. Invariants must be enforced within aggregate boundary

```typescript
// Value Object — immutable, equality by value
class Money {
  private constructor(readonly amount: number, readonly currency: Currency) {
    if (amount < 0) throw new Error("Money cannot be negative");
  }
  static of(amount: number, currency: Currency) { return new Money(amount, currency); }
  add(other: Money): Money {
    if (this.currency !== other.currency) throw new CurrencyMismatch();
    return Money.of(this.amount + other.amount, this.currency);
  }
  equals(other: Money) { return this.amount === other.amount && this.currency === other.currency; }
}

// Aggregate Root
class Order {
  private lines: OrderLine[] = [];
  private status: OrderStatus = "draft";
  private events: DomainEvent[] = [];

  addLine(product: ProductId, qty: number, price: Money): void {
    if (this.status !== "draft") throw new OrderNotEditable();
    if (this.lines.length >= 50) throw new TooManyLines();
    this.lines.push(new OrderLine(product, qty, price));
  }

  submit(): void {
    if (this.lines.length === 0) throw new EmptyOrder();
    this.status = "submitted";
    this.events.push(new OrderSubmitted(this.id, this.total()));
  }

  pullEvents(): DomainEvent[] {
    const events = [...this.events];
    this.events = [];
    return events;
  }
}
```

### Bounded Context Patterns

| Pattern | When | How |
|---|---|---|
| **Shared Kernel** | Two contexts share a small model | Shared library, versioned, co-owned |
| **Customer-Supplier** | Upstream provides, downstream consumes | Upstream publishes contract, downstream adapts |
| **Anti-Corruption Layer** | Integrating with legacy/external | Translator layer maps foreign model to yours |
| **Open Host Service** | Many consumers of your model | Published API with documented protocol |
| **Conformist** | You can't influence upstream | Just use their model directly |

## Microservices Patterns

### Decomposition
- **Strangler Fig**: Incrementally replace monolith. Route new traffic to new service, old traffic to monolith. Shrink monolith over time.
- **Branch by Abstraction**: Introduce abstraction layer, implement new behind it, switch over, remove old.

### Communication

| Pattern | When | Trade-off |
|---|---|---|
| **Sync (REST/gRPC)** | Query, need immediate response | Coupling, cascading failure |
| **Async (Events)** | Commands, notifications | Eventual consistency |
| **Saga (Orchestration)** | Multi-service transaction, central control | Single point of failure |
| **Saga (Choreography)** | Multi-service transaction, loose coupling | Hard to track, debug |
| **Outbox Pattern** | Reliable event publishing | Additional DB polling/CDC |

### Resilience Patterns

```typescript
// Circuit Breaker
class CircuitBreaker {
  private failures = 0;
  private state: "closed" | "open" | "half-open" = "closed";
  private nextAttempt = 0;

  constructor(
    private threshold: number = 5,
    private resetTimeoutMs: number = 30_000,
  ) {}

  async call<T>(fn: () => Promise<T>): Promise<T> {
    if (this.state === "open") {
      if (Date.now() < this.nextAttempt) throw new CircuitOpenError();
      this.state = "half-open";
    }
    try {
      const result = await fn();
      this.onSuccess();
      return result;
    } catch (err) {
      this.onFailure();
      throw err;
    }
  }

  private onSuccess() { this.failures = 0; this.state = "closed"; }
  private onFailure() {
    this.failures++;
    if (this.failures >= this.threshold) {
      this.state = "open";
      this.nextAttempt = Date.now() + this.resetTimeoutMs;
    }
  }
}

// Retry with exponential backoff + jitter
async function retry<T>(
  fn: () => Promise<T>,
  { maxAttempts = 3, baseDelayMs = 100, maxDelayMs = 5000 } = {},
): Promise<T> {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try { return await fn(); }
    catch (err) {
      if (attempt === maxAttempts - 1) throw err;
      const delay = Math.min(baseDelayMs * 2 ** attempt, maxDelayMs);
      const jitter = delay * (0.5 + Math.random() * 0.5);
      await new Promise(r => setTimeout(r, jitter));
    }
  }
  throw new Error("unreachable");
}

// Bulkhead — isolate resources per dependency
class Bulkhead {
  private active = 0;
  constructor(private maxConcurrent: number) {}

  async execute<T>(fn: () => Promise<T>): Promise<T> {
    if (this.active >= this.maxConcurrent) throw new BulkheadFullError();
    this.active++;
    try { return await fn(); }
    finally { this.active--; }
  }
}
```

### Sidecar / Ambassador / Anti-Corruption Layer
- **Sidecar**: Co-deployed process handling cross-cutting (logging, mTLS, config). Example: Envoy proxy, Dapr.
- **Ambassador**: Sidecar specifically for outbound traffic (retry, circuit break, routing).
- **Anti-Corruption Layer**: Translates between your domain model and external system's model. Never let external models leak in.

## CQRS + Event Sourcing

```
Command → Command Handler → Aggregate → Events → Event Store
                                                    ↓
                                              Event Handlers → Read Models (projections)
                                                    ↓
Query → Query Handler → Read Model → Response
```

```typescript
// Event Store append-only
interface EventStore {
  append(streamId: string, events: DomainEvent[], expectedVersion: number): Promise<void>;
  load(streamId: string): Promise<DomainEvent[]>;
}

// Projection builds read model from events
class OrderSummaryProjection {
  async handle(event: DomainEvent): Promise<void> {
    switch (event.type) {
      case "OrderPlaced":
        await this.db.insert("order_summaries", {
          id: event.orderId, total: event.total, status: "placed",
        });
        break;
      case "OrderShipped":
        await this.db.update("order_summaries", event.orderId, { status: "shipped" });
        break;
    }
  }
}
```

### Outbox Pattern (reliable event publishing)
1. Write aggregate state + events to same DB transaction (outbox table)
2. Background poller/CDC reads outbox table, publishes to message broker
3. Mark as published. Consumers are idempotent.

```sql
-- Outbox table
CREATE TABLE outbox (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  published_at TIMESTAMPTZ
);

CREATE INDEX idx_outbox_unpublished ON outbox (created_at) WHERE published_at IS NULL;
```

---

# 2. Frontend Engineering

## React Patterns

### Server Components vs Client Components (Next.js App Router)
```
Server Component (default):  async, fetch data, access DB, zero bundle size
Client Component ("use client"):  interactivity, hooks, browser APIs, event handlers
```

**Rule:** Server Components by default. Add `"use client"` only when you need interactivity, hooks, or browser APIs.

### Compound Components
```tsx
// Compound component — flexible composition
function Select({ children, value, onChange }: SelectProps) {
  return (
    <SelectContext.Provider value={{ value, onChange }}>
      <div role="listbox">{children}</div>
    </SelectContext.Provider>
  );
}
Select.Option = function Option({ value, children }: OptionProps) {
  const ctx = useContext(SelectContext);
  return (
    <div role="option" aria-selected={ctx.value === value}
      onClick={() => ctx.onChange(value)}>
      {children}
    </div>
  );
};
// Usage: <Select value={v} onChange={setV}><Select.Option value="a">A</Select.Option></Select>
```

### Custom Hooks — Reusable Logic
```typescript
// Debounced value
function useDebounce<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

// Previous value
function usePrevious<T>(value: T): T | undefined {
  const ref = useRef<T>();
  useEffect(() => { ref.current = value; });
  return ref.current;
}

// Media query
function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [query]);
  return matches;
}
```

### Error Boundaries
```tsx
class ErrorBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { error?: Error }> {
  state = { error: undefined as Error | undefined };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { reportError(error, info); }
  render() {
    return this.state.error ? this.props.fallback : this.props.children;
  }
}
// Usage: <ErrorBoundary fallback={<ErrorPage />}><App /></ErrorBoundary>
```

### Suspense + Streaming
```tsx
// Next.js App Router — streaming with Suspense
export default function Page() {
  return (
    <main>
      <h1>Dashboard</h1>
      <Suspense fallback={<Skeleton />}>
        <AsyncStats />  {/* Server Component, streams in when ready */}
      </Suspense>
      <Suspense fallback={<TableSkeleton />}>
        <AsyncTable />
      </Suspense>
    </main>
  );
}
```

## Next.js App Router

### Route Architecture
```
app/
├── layout.tsx           # Root layout (shared UI, providers)
├── page.tsx             # Home page
├── loading.tsx          # Suspense fallback for this segment
├── error.tsx            # Error boundary for this segment
├── not-found.tsx        # 404 for this segment
├── (marketing)/         # Route group (no URL impact)
│   ├── about/page.tsx
│   └── pricing/page.tsx
├── dashboard/
│   ├── layout.tsx       # Nested layout (persists across nav)
│   ├── page.tsx
│   └── [teamId]/        # Dynamic segment
│       └── page.tsx
└── api/                 # Route handlers
    └── webhooks/
        └── stripe/route.ts
```

### Server Actions
```typescript
// app/actions.ts
"use server";

import { revalidatePath } from "next/cache";
import { z } from "zod";

const CreatePostSchema = z.object({
  title: z.string().min(1).max(200),
  body: z.string().min(1),
});

export async function createPost(formData: FormData) {
  const parsed = CreatePostSchema.safeParse(Object.fromEntries(formData));
  if (!parsed.success) return { error: parsed.error.flatten() };
  
  await db.post.create({ data: parsed.data });
  revalidatePath("/posts");
  redirect("/posts");
}
```

### Middleware
```typescript
// middleware.ts — runs on Edge Runtime
import { NextResponse } from "next/server";
export function middleware(request: NextRequest) {
  const token = request.cookies.get("session");
  if (!token && request.nextUrl.pathname.startsWith("/dashboard")) {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  // Add request ID for tracing
  const headers = new Headers(request.headers);
  headers.set("x-request-id", crypto.randomUUID());
  return NextResponse.next({ request: { headers } });
}
export const config = { matcher: ["/dashboard/:path*", "/api/:path*"] };
```

## State Management

| Tool | Scope | When |
|---|---|---|
| **TanStack Query** | Server state (async data) | API calls, cache, revalidation |
| **Zustand** | Client state (simple) | UI state, feature flags, preferences |
| **Jotai** | Client state (atomic) | Many independent atoms, derived state |
| **URL params (nuqs)** | Shareable state | Filters, pagination, search |
| **React Hook Form** | Form state | All forms (never controlled inputs for big forms) |

### TanStack Query Patterns
```typescript
// Query with cache
const { data, isLoading, error } = useQuery({
  queryKey: ["users", filters],    // cache key — auto refetch when filters change
  queryFn: () => api.users.list(filters),
  staleTime: 5 * 60 * 1000,       // data fresh for 5 min
  gcTime: 30 * 60 * 1000,         // garbage collect after 30 min
});

// Mutation with optimistic update
const mutation = useMutation({
  mutationFn: api.users.update,
  onMutate: async (updated) => {
    await queryClient.cancelQueries({ queryKey: ["users"] });
    const previous = queryClient.getQueryData(["users"]);
    queryClient.setQueryData(["users"], (old: User[]) =>
      old.map(u => u.id === updated.id ? { ...u, ...updated } : u)
    );
    return { previous };
  },
  onError: (_err, _vars, context) => {
    queryClient.setQueryData(["users"], context?.previous);
  },
  onSettled: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
});

// Dependent queries
const { data: user } = useQuery({ queryKey: ["user", userId], queryFn: () => fetchUser(userId) });
const { data: orders } = useQuery({
  queryKey: ["orders", user?.id],
  queryFn: () => fetchOrders(user!.id),
  enabled: !!user,   // only runs after user loads
});
```

### Zustand
```typescript
interface AuthStore {
  user: User | null;
  login: (creds: Credentials) => Promise<void>;
  logout: () => void;
}

const useAuthStore = create<AuthStore>()((set) => ({
  user: null,
  login: async (creds) => {
    const user = await api.auth.login(creds);
    set({ user });
  },
  logout: () => set({ user: null }),
}));

// Selector (avoids re-render when unrelated state changes)
const userName = useAuthStore((s) => s.user?.name);
```

## Frontend Performance

### Core Web Vitals Targets
| Metric | Good | What |
|---|---|---|
| **LCP** | < 2.5s | Largest Contentful Paint — main content visible |
| **INP** | < 200ms | Interaction to Next Paint — responsiveness |
| **CLS** | < 0.1 | Cumulative Layout Shift — visual stability |

### Optimization Checklist
- **Code splitting**: `React.lazy()`, dynamic `import()`, route-based splitting
- **Images**: `next/image` (auto WebP/AVIF, responsive sizes, lazy load, blur placeholder)
- **Fonts**: `next/font` (zero layout shift, self-hosted, font-display: swap)
- **Virtualization**: `@tanstack/react-virtual` for long lists (never render 1000+ DOM nodes)
- **Bundle**: Analyze with `@next/bundle-analyzer`. Tree-shake unused exports. No barrel imports from heavy packages.
- **Memoization**: `useMemo` for expensive computation, `React.memo` for pure components that re-render often. Do NOT memo everything blindly.
- **Web Workers**: Offload CPU-heavy work (parsing, crypto, image processing) to workers
- **Service Workers**: Offline support, background sync, cache strategies (stale-while-revalidate)

### Anti-patterns
- Fetching in `useEffect` without cancellation (race conditions). Use TanStack Query or `use()`.
- Prop drilling through 5+ levels. Use composition, context, or state library.
- Storing derived state in useState. Compute it during render.
- `index` as key in lists that reorder.

## CSS — Tailwind + Modern CSS

```tsx
// Design tokens via CSS variables + Tailwind
// tailwind.config.ts
export default {
  theme: {
    extend: {
      colors: { brand: "hsl(var(--brand) / <alpha-value>)" },
      spacing: { "page-gutter": "var(--page-gutter)" },
    },
  },
}

// Container queries (component-level responsiveness)
// <div className="@container">
//   <div className="@lg:grid-cols-2 @sm:grid-cols-1">

// CSS Subgrid (align nested grids to parent)
// .parent { display: grid; grid-template-columns: repeat(3, 1fr); }
// .child  { display: grid; grid-template-columns: subgrid; grid-column: span 3; }
```

## Accessibility (WCAG 2.1 AA)

### Must-haves
- Semantic HTML: `<button>` not `<div onClick>`. `<nav>`, `<main>`, `<article>`.
- All images have `alt` text (decorative = `alt=""`)
- Focus management: visible focus ring, logical tab order, skip links
- ARIA only when semantic HTML is insufficient: `role`, `aria-label`, `aria-expanded`, `aria-live`
- Color contrast: 4.5:1 normal text, 3:1 large text
- Keyboard navigation: all interactive elements operable via keyboard alone
- Screen reader testing: VoiceOver (macOS), NVDA (Windows)
- `prefers-reduced-motion`: disable animations for users who request it
- `prefers-color-scheme`: support dark mode

### Focus Management
```typescript
// After navigation or modal open, move focus
const headingRef = useRef<HTMLHeadingElement>(null);
useEffect(() => { headingRef.current?.focus(); }, [page]);

// Trap focus in modal
function useFocusTrap(containerRef: RefObject<HTMLElement>) {
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const focusable = container.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    const first = focusable[0], last = focusable[focusable.length - 1];
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    container.addEventListener("keydown", handler);
    first?.focus();
    return () => container.removeEventListener("keydown", handler);
  }, [containerRef]);
}
```

## Mobile Development

### React Native / Expo
```typescript
// File-based routing (Expo Router)
// app/(tabs)/_layout.tsx
export default function TabLayout() {
  return (
    <Tabs>
      <Tabs.Screen name="index" options={{ title: "Home", tabBarIcon: HomeIcon }} />
      <Tabs.Screen name="profile" options={{ title: "Profile", tabBarIcon: ProfileIcon }} />
    </Tabs>
  );
}

// Offline-first with sync
import * as SQLite from "expo-sqlite";
const db = SQLite.openDatabaseAsync("app.db");
// Queue mutations locally, sync when online
// Use NetInfo to detect connectivity, batch sync pending operations
```

### Performance Rules
- `FlashList` for lists (not FlatList, never ScrollView for long lists)
- `expo-image` with caching + blur placeholders
- Minimize bridge crossings. Use JSI/TurboModules for native calls.
- Hermes engine (default in Expo SDK 49+)
- Avoid inline styles in render (create with `StyleSheet.create`)

### Flutter
```dart
// Riverpod state management (preferred over Provider)
final userProvider = FutureProvider<User>((ref) => api.fetchUser());

class ProfileScreen extends ConsumerWidget {
  Widget build(BuildContext context, WidgetRef ref) {
    final user = ref.watch(userProvider);
    return user.when(
      data: (u) => Text(u.name),
      loading: () => CircularProgressIndicator(),
      error: (e, _) => Text("Error: $e"),
    );
  }
}
```

---

# 3. Backend Engineering

## Node.js / TypeScript

### Fastify (preferred over Express for performance)
```typescript
import Fastify from "fastify";

const app = Fastify({ logger: { level: "info" } });

// Type-safe route
app.post<{
  Body: CreateUserBody;
  Reply: { 200: User; 400: ApiError };
}>("/users", {
  schema: {
    body: CreateUserBodySchema,
    response: { 200: UserSchema, 400: ApiErrorSchema },
  },
  handler: async (request, reply) => {
    const result = await userService.create(request.body);
    if (!result.ok) return reply.code(400).send(result.error);
    return reply.code(200).send(result.data);
  },
});

// Graceful shutdown
const signals = ["SIGINT", "SIGTERM"];
for (const signal of signals) {
  process.on(signal, async () => {
    app.log.info(`Received ${signal}, shutting down gracefully`);
    await app.close();  // drains connections
    process.exit(0);
  });
}
```

### Event Loop — What Blocks and What Doesn't
| Blocks | Doesn't Block |
|---|---|
| `JSON.parse` (large) | `fs.readFile` (async) |
| `crypto.pbkdf2Sync` | `crypto.pbkdf2` (callback) |
| `RegExp` backtracking | Database queries (async) |
| Tight loops (>10ms) | HTTP requests (async) |
| `fs.readFileSync` | Timers, I/O callbacks |

**Rule:** Anything CPU-bound >1ms — offload to worker thread or external service.

```typescript
// Worker threads for CPU-heavy work
import { Worker } from "worker_threads";
function runInWorker<T>(workerFile: string, data: unknown): Promise<T> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(workerFile, { workerData: data });
    worker.on("message", resolve);
    worker.on("error", reject);
  });
}
```

### Streams (process data without loading all into memory)
```typescript
import { pipeline } from "stream/promises";
import { createReadStream, createWriteStream } from "fs";
import { Transform } from "stream";

const csvToJson = new Transform({
  objectMode: true,
  transform(chunk, _enc, callback) {
    const lines = chunk.toString().split("\n");
    for (const line of lines) {
      this.push(JSON.stringify(parseCsvLine(line)) + "\n");
    }
    callback();
  },
});

await pipeline(
  createReadStream("data.csv"),
  csvToJson,
  createWriteStream("data.jsonl"),
);
```

## Go

### Core Patterns
```go
// Context propagation — ALWAYS pass context
func (s *OrderService) PlaceOrder(ctx context.Context, req PlaceOrderReq) (*Order, error) {
    ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
    defer cancel()

    order, err := s.repo.Save(ctx, req.toOrder())
    if err != nil {
        return nil, fmt.Errorf("save order %s: %w", req.ID, err)  // wrap with context
    }
    return order, nil
}

// Interface — accept interfaces, return structs
type OrderRepository interface {
    Save(ctx context.Context, order *Order) error
    FindByID(ctx context.Context, id string) (*Order, error)
}

type postgresOrderRepo struct { db *pgxpool.Pool }
func (r *postgresOrderRepo) Save(ctx context.Context, order *Order) error { /* ... */ }

// Table-driven tests
func TestParseAmount(t *testing.T) {
    tests := []struct {
        name    string
        input   string
        want    int64
        wantErr bool
    }{
        {"valid", "42.50", 4250, false},
        {"negative", "-10.00", -1000, false},
        {"invalid", "abc", 0, true},
        {"overflow", "999999999999", 0, true},
    }
    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            got, err := ParseAmount(tt.input)
            if (err != nil) != tt.wantErr { t.Fatalf("err = %v, wantErr %v", err, tt.wantErr) }
            if got != tt.want { t.Errorf("got %d, want %d", got, tt.want) }
        })
    }
}

// Goroutines + errgroup for concurrent work
func FetchAll(ctx context.Context, ids []string) ([]Item, error) {
    g, ctx := errgroup.WithContext(ctx)
    results := make([]Item, len(ids))
    for i, id := range ids {
        i, id := i, id  // capture
        g.Go(func() error {
            item, err := fetchItem(ctx, id)
            if err != nil { return err }
            results[i] = item
            return nil
        })
    }
    if err := g.Wait(); err != nil { return nil, err }
    return results, nil
}

// Error wrapping (Go 1.13+)
var ErrNotFound = errors.New("not found")

func (r *repo) FindByID(ctx context.Context, id string) (*User, error) {
    row := r.db.QueryRowContext(ctx, "SELECT ... WHERE id = $1", id)
    var u User
    if err := row.Scan(&u.ID, &u.Name); err != nil {
        if errors.Is(err, sql.ErrNoRows) { return nil, fmt.Errorf("user %s: %w", id, ErrNotFound) }
        return nil, fmt.Errorf("query user %s: %w", id, err)
    }
    return &u, nil
}
```

## Python

### FastAPI + Pydantic v2
```python
from fastapi import FastAPI, Depends, HTTPException, status
from pydantic import BaseModel, Field, EmailStr
from typing import Annotated
from uuid import UUID

app = FastAPI()

# Pydantic v2 models — schema validation at boundary
class CreateUserRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "member"] = "member"

class UserResponse(BaseModel):
    id: UUID
    email: str
    name: str
    model_config = ConfigDict(from_attributes=True)  # ORM mode

# Dependency injection
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session

@app.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user = User(**body.model_dump())
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user

# SQLAlchemy 2.0 async
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]
    hashed_password: Mapped[str]

# Celery for background tasks
from celery import Celery
celery_app = Celery("worker", broker="redis://localhost:6379/0")

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_welcome_email(self, user_id: str):
    try:
        user = get_user(user_id)
        email_service.send(user.email, "Welcome!")
    except TransientError as e:
        self.retry(exc=e)
```

## Rust (systems-level performance)

```rust
// Axum web framework
use axum::{extract::State, routing::post, Json, Router};
use serde::{Deserialize, Serialize};

#[derive(Clone)]
struct AppState { db: PgPool }

#[derive(Deserialize)]
struct CreateUser { email: String, name: String }

#[derive(Serialize)]
struct UserResponse { id: Uuid, email: String, name: String }

async fn create_user(
    State(state): State<AppState>,
    Json(body): Json<CreateUser>,
) -> Result<Json<UserResponse>, AppError> {
    let user = sqlx::query_as!(UserResponse,
        "INSERT INTO users (email, name) VALUES ($1, $2) RETURNING id, email, name",
        body.email, body.name
    ).fetch_one(&state.db).await?;
    Ok(Json(user))
}

// Error handling with thiserror
#[derive(Debug, thiserror::Error)]
enum AppError {
    #[error("not found: {0}")]
    NotFound(String),
    #[error("database error")]
    Database(#[from] sqlx::Error),
    #[error("validation: {0}")]
    Validation(String),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            AppError::NotFound(msg) => (StatusCode::NOT_FOUND, msg.clone()),
            AppError::Database(_) => (StatusCode::INTERNAL_SERVER_ERROR, "internal error".into()),
            AppError::Validation(msg) => (StatusCode::BAD_REQUEST, msg.clone()),
        };
        (status, Json(json!({ "error": message }))).into_response()
    }
}
```

## Database — PostgreSQL Deep Patterns

### Advanced Queries
```sql
-- CTE (Common Table Expression) — readable complex queries
WITH monthly_revenue AS (
  SELECT date_trunc('month', created_at) AS month,
         SUM(amount) AS revenue,
         COUNT(*) AS order_count
  FROM orders
  WHERE status = 'completed'
  GROUP BY 1
),
growth AS (
  SELECT month, revenue, order_count,
         LAG(revenue) OVER (ORDER BY month) AS prev_revenue,
         revenue - LAG(revenue) OVER (ORDER BY month) AS growth
  FROM monthly_revenue
)
SELECT month, revenue, order_count, growth,
       ROUND(growth / NULLIF(prev_revenue, 0) * 100, 1) AS growth_pct
FROM growth ORDER BY month;

-- Window functions
SELECT user_id, amount, created_at,
       SUM(amount) OVER (PARTITION BY user_id ORDER BY created_at) AS running_total,
       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY amount DESC) AS rank_by_amount,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) OVER (PARTITION BY user_id) AS median
FROM orders;

-- JSONB queries (PostgreSQL)
SELECT * FROM products
WHERE metadata @> '{"category": "electronics"}'   -- contains
  AND metadata->>'brand' ILIKE '%apple%'           -- text search in JSON
  AND (metadata->'specs'->'ram')::int >= 16;       -- nested numeric comparison

-- Upsert (INSERT ... ON CONFLICT)
INSERT INTO user_preferences (user_id, key, value, updated_at)
VALUES ($1, $2, $3, now())
ON CONFLICT (user_id, key)
DO UPDATE SET value = EXCLUDED.value, updated_at = now()
RETURNING *;

-- Partitioning (for tables > 100M rows)
CREATE TABLE events (
  id UUID DEFAULT gen_random_uuid(),
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);

CREATE TABLE events_2024_q1 PARTITION OF events
  FOR VALUES FROM ('2024-01-01') TO ('2024-04-01');

-- Full-text search
ALTER TABLE articles ADD COLUMN search_vector tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(body, '')), 'B')
  ) STORED;
CREATE INDEX idx_articles_search ON articles USING GIN (search_vector);
SELECT *, ts_rank(search_vector, query) AS rank
FROM articles, plainto_tsquery('english', 'database optimization') AS query
WHERE search_vector @@ query ORDER BY rank DESC;
```

### Index Strategy

| Index Type | Use Case | Example |
|---|---|---|
| **B-tree** (default) | Equality, range, sorting | `WHERE id = $1`, `ORDER BY created_at` |
| **GIN** | Full-text search, JSONB, arrays | `WHERE tags @> ARRAY['go']` |
| **GiST** | Geometric, range, nearest-neighbor | PostGIS, `WHERE tsrange && ...` |
| **BRIN** | Very large tables, naturally ordered | Time-series append-only data |
| **Partial** | Filtered subset | `WHERE status = 'active'` (only index actives) |
| **Covering** | Index-only scans | `INCLUDE (name, email)` to avoid heap access |
| **Composite** | Multi-column queries | `(user_id, created_at DESC)` |

```sql
-- Partial index (only index what you query)
CREATE INDEX idx_orders_pending ON orders (created_at)
  WHERE status = 'pending';

-- Covering index (index-only scan, no table access)
CREATE INDEX idx_users_email_cover ON users (email) INCLUDE (name, avatar_url);

-- Expression index
CREATE INDEX idx_users_lower_email ON users (lower(email));

-- EXPLAIN ANALYZE — always check your queries
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT * FROM orders WHERE user_id = $1 AND status = 'active' ORDER BY created_at DESC LIMIT 20;
-- Look for: Seq Scan (bad on large tables), Nested Loop (potential N+1), high actual rows vs estimate
```

### Zero-Downtime Migration (Expand-Contract)
1. **Expand**: Add new column (nullable), deploy code that writes to both
2. **Migrate**: Backfill existing data
3. **Switch**: Deploy code that reads from new column
4. **Contract**: Drop old column

```sql
-- Step 1: Add nullable column (instant, no lock)
ALTER TABLE users ADD COLUMN full_name TEXT;

-- Step 2: Backfill (batched, no lock on whole table)
UPDATE users SET full_name = first_name || ' ' || last_name
WHERE full_name IS NULL AND id > $last_id
LIMIT 10000;  -- repeat in batches

-- Step 3: Deploy code reading from full_name
-- Step 4: Make NOT NULL (after all rows populated)
ALTER TABLE users ALTER COLUMN full_name SET NOT NULL;
-- Step 5: Drop old columns (after full deployment)
ALTER TABLE users DROP COLUMN first_name, DROP COLUMN last_name;
```

### Redis Patterns

| Pattern | Use | Implementation |
|---|---|---|
| **Cache-Aside** | Read-heavy, tolerance for stale | Read cache → miss → read DB → write cache |
| **Write-Through** | Strong consistency needed | Write DB + cache atomically |
| **Write-Behind** | High write throughput | Write cache → async flush to DB |
| **Distributed Lock** | Mutual exclusion | `SET key value NX EX 30` + Redlock |
| **Rate Limiter** | API rate limiting | Sliding window with sorted sets |
| **Pub/Sub** | Real-time notifications | Lightweight, no persistence |
| **Streams** | Event log with consumer groups | Persistent, exactly-once via XACK |

```typescript
// Cache-aside with stampede protection
async function getUser(id: string): Promise<User> {
  const cached = await redis.get(`user:${id}`);
  if (cached) return JSON.parse(cached);
  
  // Distributed lock to prevent thundering herd
  const lock = await redis.set(`lock:user:${id}`, "1", "NX", "EX", 5);
  if (!lock) {
    await sleep(50);
    return getUser(id);  // retry after brief wait
  }
  
  const user = await db.users.findById(id);
  await redis.set(`user:${id}`, JSON.stringify(user), "EX", 300);
  await redis.del(`lock:user:${id}`);
  return user;
}

// Sliding window rate limiter
async function isRateLimited(key: string, limit: number, windowSec: number): Promise<boolean> {
  const now = Date.now();
  const pipe = redis.pipeline();
  pipe.zremrangebyscore(key, 0, now - windowSec * 1000);
  pipe.zadd(key, now, `${now}-${Math.random()}`);
  pipe.zcard(key);
  pipe.expire(key, windowSec);
  const results = await pipe.exec();
  const count = results[2][1] as number;
  return count > limit;
}
```

### ORM Decision Matrix

| ORM | Language | Strengths | Use When |
|---|---|---|---|
| **Prisma** | TypeScript | Type-safe, great DX, migrations | Most TS projects |
| **Drizzle** | TypeScript | SQL-like, lightweight, fast | Need SQL control + types |
| **SQLAlchemy 2.0** | Python | Mature, flexible, async | Python projects |
| **GORM** | Go | Simple, hooks | Simple Go CRUD |
| **sqlx** | Go/Rust | Compile-time SQL checking | Complex queries, performance |
| **Raw SQL** | Any | Full control | Complex analytics, performance-critical |

---

# 4. API Design

## REST — Richardson Maturity Model

| Level | What | Example |
|---|---|---|
| 0 | Single endpoint, RPC-style | `POST /api` with action in body |
| 1 | Resources | `POST /orders`, `GET /users/123` |
| 2 | HTTP verbs + status codes | `GET`, `POST`, `PUT`, `DELETE`, `PATCH` + `201`, `404` |
| 3 | HATEOAS (hypermedia links) | Response includes `_links` for discoverability |

### Resource Design
```
# Nouns, not verbs. Plural. Hierarchical.
GET    /users                    # List users (paginated)
POST   /users                    # Create user
GET    /users/{id}               # Get user
PATCH  /users/{id}               # Partial update
DELETE /users/{id}               # Delete user
GET    /users/{id}/orders        # User's orders (sub-resource)
POST   /users/{id}/orders        # Create order for user

# Filtering, sorting, pagination via query params
GET /orders?status=active&sort=-created_at&limit=20&cursor=eyJpZCI6MTIzfQ
```

### Status Codes — Use Correctly

| Code | When | Never |
|---|---|---|
| **200** | Successful GET/PATCH/DELETE | For creation (use 201) |
| **201** | Created (POST), return Location header | Without Location header |
| **204** | Success, no body (DELETE) | When client needs confirmation data |
| **400** | Validation error (client's fault) | For auth errors (use 401/403) |
| **401** | Not authenticated | When authenticated but unauthorized (use 403) |
| **403** | Authenticated but forbidden | When not authenticated (use 401) |
| **404** | Resource not found | To hide existence (use 403 for security) |
| **409** | Conflict (duplicate, version mismatch) | For validation (use 400) |
| **422** | Semantic validation error | (Some prefer 400 for all validation) |
| **429** | Rate limited, include Retry-After | Without Retry-After header |
| **500** | Server error (never expose internals) | With stack traces in production |

### Error Response (RFC 7807)
```json
{
  "type": "https://api.example.com/errors/validation",
  "title": "Validation Error",
  "status": 400,
  "detail": "Request body contains invalid fields",
  "instance": "/users/123",
  "errors": [
    { "field": "email", "message": "must be a valid email address" },
    { "field": "age", "message": "must be at least 18" }
  ],
  "traceId": "abc-123-def"
}
```

### Pagination (Cursor-based > Offset-based)
```typescript
// Cursor-based pagination (stable, performant)
interface PaginatedResponse<T> {
  data: T[];
  cursor: string | null;     // null = no more pages
  hasMore: boolean;
}

// Backend
async function listOrders(userId: string, cursor?: string, limit = 20) {
  const where = cursor
    ? `WHERE user_id = $1 AND id < $2 ORDER BY id DESC LIMIT $3`
    : `WHERE user_id = $1 ORDER BY id DESC LIMIT $2`;
  const params = cursor ? [userId, decodeCursor(cursor), limit + 1] : [userId, limit + 1];
  const rows = await db.query(where, params);
  const hasMore = rows.length > limit;
  const data = rows.slice(0, limit);
  return { data, cursor: hasMore ? encodeCursor(data.at(-1)!.id) : null, hasMore };
}
```

## GraphQL

```graphql
# Schema-first design
type Query {
  user(id: ID!): User
  users(first: Int = 20, after: String, filter: UserFilter): UserConnection!
}

type Mutation {
  createUser(input: CreateUserInput!): CreateUserPayload!
}

type User {
  id: ID!
  email: String!
  orders(first: Int = 10, after: String): OrderConnection!
}

# Relay-style pagination
type UserConnection {
  edges: [UserEdge!]!
  pageInfo: PageInfo!
  totalCount: Int!
}

type UserEdge {
  cursor: String!
  node: User!
}

type PageInfo {
  hasNextPage: Boolean!
  endCursor: String
}

# Input types for mutations
input CreateUserInput {
  email: String!
  name: String!
}

# Payload pattern (union for success/error)
type CreateUserPayload {
  user: User
  errors: [UserError!]
}
```

### DataLoader (N+1 Prevention)
```typescript
// Without DataLoader: N+1 — one query per user's orders
// With DataLoader: batches into single query
const orderLoader = new DataLoader<string, Order[]>(async (userIds) => {
  const orders = await db.query("SELECT * FROM orders WHERE user_id = ANY($1)", [userIds]);
  const byUser = groupBy(orders, "user_id");
  return userIds.map(id => byUser[id] ?? []);
});

// In resolver
const resolvers = {
  User: {
    orders: (user) => orderLoader.load(user.id),  // batched automatically
  },
};
```

## gRPC

```protobuf
syntax = "proto3";

service OrderService {
  rpc CreateOrder(CreateOrderRequest) returns (Order);              // Unary
  rpc ListOrders(ListOrdersRequest) returns (stream Order);        // Server streaming
  rpc UploadOrders(stream Order) returns (UploadResult);           // Client streaming
  rpc SyncOrders(stream SyncRequest) returns (stream SyncResponse); // Bidirectional
}

message Order {
  string id = 1;
  string user_id = 2;
  repeated LineItem items = 3;
  google.protobuf.Timestamp created_at = 4;
  OrderStatus status = 5;
}

enum OrderStatus {
  ORDER_STATUS_UNSPECIFIED = 0;
  ORDER_STATUS_PENDING = 1;
  ORDER_STATUS_COMPLETED = 2;
}
```

## tRPC (end-to-end type safety)
```typescript
// Server
const appRouter = router({
  user: router({
    get: publicProcedure
      .input(z.object({ id: z.string().uuid() }))
      .query(async ({ input }) => db.user.findUnique({ where: { id: input.id } })),
    create: protectedProcedure
      .input(CreateUserSchema)
      .mutation(async ({ input, ctx }) => db.user.create({ data: { ...input, orgId: ctx.org.id } })),
  }),
});

// Client — fully typed, no codegen
const user = trpc.user.get.useQuery({ id: "123" });
const mutation = trpc.user.create.useMutation();
```

## WebSocket / SSE

```typescript
// SSE — server push, simpler than WebSocket for one-way data
// Server (Next.js Route Handler)
export async function GET(request: Request) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      const send = (data: unknown) => {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(data)}\n\n`));
      };
      // Subscribe to events
      const unsub = eventBus.subscribe("order:*", send);
      request.signal.addEventListener("abort", () => { unsub(); controller.close(); });
    },
  });
  return new Response(stream, {
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  });
}

// Client
const es = new EventSource("/api/events");
es.onmessage = (e) => { const data = JSON.parse(e.data); /* update state */ };
```

---

# 5. Type Safety & Language Patterns

## TypeScript — Advanced Patterns

### Branded Types (prevent ID mixing)
```typescript
type Brand<T, B extends string> = T & { __brand: B };
type UserId = Brand<string, "UserId">;
type OrderId = Brand<string, "OrderId">;

function createUserId(id: string): UserId { return id as UserId; }

// Compile error: can't pass UserId where OrderId expected
function getOrder(id: OrderId): Order { /* ... */ }
getOrder(createUserId("123"));  // TS Error!
```

### Discriminated Unions (state machines)
```typescript
type AsyncState<T, E = Error> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; data: T }
  | { status: "error"; error: E };

function render(state: AsyncState<User>) {
  switch (state.status) {
    case "idle": return <Placeholder />;
    case "loading": return <Spinner />;
    case "success": return <Profile user={state.data} />;  // data is typed
    case "error": return <Error error={state.error} />;    // error is typed
    default: return exhaustive(state);  // compile error if case missing
  }
}
function exhaustive(x: never): never { throw new Error(`Unhandled: ${x}`); }
```

### Template Literal Types
```typescript
type EventName = `${string}:${"created" | "updated" | "deleted"}`;
type Route = `/${string}`;

type CSSUnit = `${number}${"px" | "rem" | "em" | "%"}`;
const width: CSSUnit = "100px";  // OK
const bad: CSSUnit = "100vw";    // Error
```

### Conditional Types + Infer
```typescript
// Extract return type of async function
type AsyncReturnType<T extends (...args: any) => Promise<any>> =
  T extends (...args: any) => Promise<infer R> ? R : never;

// Make specific keys required
type WithRequired<T, K extends keyof T> = T & { [P in K]-?: T[P] };

// Deep partial
type DeepPartial<T> = T extends object ? { [P in keyof T]?: DeepPartial<T[P]> } : T;

// Mapped types — create API response types from domain
type ApiResponse<T> = {
  [K in keyof T as T[K] extends Function ? never : K]: T[K] extends Date ? string : T[K];
};
```

### Zod — Schema Validation at Boundaries
```typescript
import { z } from "zod";

const UserSchema = z.object({
  email: z.string().email(),
  name: z.string().min(1).max(100),
  age: z.number().int().min(18).max(150).optional(),
  role: z.enum(["admin", "member", "viewer"]),
  metadata: z.record(z.string(), z.unknown()).optional(),
});

type User = z.infer<typeof UserSchema>;  // infer TS type from schema

// Transform + refine
const DateRangeSchema = z.object({
  start: z.string().datetime().transform(s => new Date(s)),
  end: z.string().datetime().transform(s => new Date(s)),
}).refine(
  (d) => d.end > d.start,
  { message: "end must be after start", path: ["end"] },
);

// Discriminated union schema
const PaymentSchema = z.discriminatedUnion("method", [
  z.object({ method: z.literal("card"), cardToken: z.string() }),
  z.object({ method: z.literal("bank"), accountNumber: z.string(), routingNumber: z.string() }),
  z.object({ method: z.literal("crypto"), walletAddress: z.string(), chain: z.enum(["eth", "btc"]) }),
]);
```

## Go — Type Patterns

```go
// Result pattern in Go
type Result[T any] struct {
    Value T
    Err   error
}

func Ok[T any](v T) Result[T]    { return Result[T]{Value: v} }
func Fail[T any](e error) Result[T] { return Result[T]{Err: e} }

// Functional options pattern
type ServerOption func(*Server)

func WithPort(port int) ServerOption { return func(s *Server) { s.port = port } }
func WithTimeout(d time.Duration) ServerOption { return func(s *Server) { s.timeout = d } }

func NewServer(opts ...ServerOption) *Server {
    s := &Server{port: 8080, timeout: 30 * time.Second}  // defaults
    for _, opt := range opts { opt(s) }
    return s
}
// Usage: server := NewServer(WithPort(9090), WithTimeout(60*time.Second))

// Interface embedding + composition
type ReadWriter interface {
    Reader
    Writer
}
```

## Python — Type Patterns

```python
from typing import TypeVar, Generic, Protocol, Literal
from pydantic import BaseModel, field_validator
from dataclasses import dataclass

# Protocol (structural typing — like Go interfaces)
class Repository(Protocol[T]):
    async def save(self, entity: T) -> None: ...
    async def find_by_id(self, id: str) -> T | None: ...

# Generic result type
T = TypeVar("T")
E = TypeVar("E", bound=Exception)

@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T
    @property
    def is_ok(self) -> bool: return True

@dataclass(frozen=True)
class Err(Generic[E]):
    error: E
    @property
    def is_ok(self) -> bool: return False

Result = Ok[T] | Err[E]

# Pydantic v2 — computed fields, validators
class Order(BaseModel):
    items: list[OrderItem]
    discount_pct: float = Field(ge=0, le=100, default=0)

    @computed_field
    @property
    def subtotal(self) -> Decimal:
        return sum(item.price * item.qty for item in self.items)

    @computed_field
    @property
    def total(self) -> Decimal:
        return self.subtotal * (1 - Decimal(self.discount_pct) / 100)

    @field_validator("items")
    @classmethod
    def at_least_one_item(cls, v: list) -> list:
        if not v: raise ValueError("order must have at least one item")
        return v
```

---

# 6. Testing

## Strategy

```
                    ┌──────────────────┐
                    │  E2E (Playwright) │  ← Few, slow, high confidence
                    ├──────────────────┤
                    │   Integration     │  ← More, medium speed
                    │  (API, DB, Queue) │
                    ├──────────────────┤
                    │    Unit Tests     │  ← Many, fast, low confidence
                    └──────────────────┘
```

**Testing Trophy** (Kent C. Dodds): Emphasize integration tests — they give best ROI. Unit tests for complex logic only. E2E for critical user flows.

## Unit Testing

### Factory Pattern for Test Data
```typescript
// Never construct raw objects in tests. Use factories.
function createUser(overrides: Partial<User> = {}): User {
  return {
    id: randomUUID(),
    email: `user-${randomUUID().slice(0, 8)}@test.com`,
    name: "Test User",
    role: "member",
    createdAt: new Date(),
    ...overrides,
  };
}

// Usage
const admin = createUser({ role: "admin" });
const users = Array.from({ length: 5 }, () => createUser());
```

### Test Doubles
| Type | What | When |
|---|---|---|
| **Stub** | Returns canned data | Isolate from dependency |
| **Spy** | Records calls for assertion | Verify interaction |
| **Mock** | Stub + verification | Only at boundaries (HTTP, DB, queue) |
| **Fake** | Working implementation (in-memory DB) | Integration tests |

```typescript
// Mock at boundary ONLY
const mockPaymentGateway: PaymentGateway = {
  charge: vi.fn().mockResolvedValue({ ok: true, transactionId: "txn_123" }),
};

// Fake (working in-memory implementation)
class InMemoryUserRepo implements UserRepository {
  private users = new Map<string, User>();
  async save(user: User) { this.users.set(user.id, user); }
  async findById(id: string) { return this.users.get(id) ?? null; }
  async findByEmail(email: string) { return [...this.users.values()].find(u => u.email === email) ?? null; }
}
```

### Property-Based Testing
```typescript
import { fc } from "@fast-check/vitest";

// Instead of specific examples, test PROPERTIES that must always hold
test.prop([fc.emailAddress()])("parsed email roundtrips", (email) => {
  const parsed = parseEmail(email);
  expect(parsed.toString()).toBe(email.toLowerCase());
});

test.prop([fc.integer(), fc.integer()])("addition is commutative", (a, b) => {
  expect(add(a, b)).toBe(add(b, a));
});

test.prop([fc.array(fc.integer())])("sort is idempotent", (arr) => {
  const once = sort(arr);
  const twice = sort(sort(arr));
  expect(once).toEqual(twice);
});
```

## Integration Testing

### TestContainers (real DB in tests)
```typescript
import { PostgreSqlContainer } from "@testcontainers/postgresql";

let container: StartedPostgreSqlContainer;
let db: Pool;

beforeAll(async () => {
  container = await new PostgreSqlContainer().start();
  db = new Pool({ connectionString: container.getConnectionUri() });
  await migrate(db);  // run migrations
}, 30_000);

afterAll(async () => {
  await db.end();
  await container.stop();
});

// Each test gets clean state
beforeEach(async () => {
  await db.query("TRUNCATE users, orders CASCADE");
});

test("creates user and returns with ID", async () => {
  const user = await userService.create({ email: "test@example.com", name: "Test" });
  expect(user.id).toBeDefined();
  
  const found = await db.query("SELECT * FROM users WHERE id = $1", [user.id]);
  expect(found.rows[0].email).toBe("test@example.com");
});
```

### API Contract Testing (Pact)
```typescript
// Consumer defines expectations
const interaction = {
  state: "user 123 exists",
  uponReceiving: "a request for user 123",
  withRequest: { method: "GET", path: "/users/123" },
  willRespondWith: {
    status: 200,
    body: like({ id: "123", email: string(), name: string() }),
  },
};

// Provider verifies it can fulfill the contract
// This prevents breaking changes between services
```

## E2E Testing (Playwright)

```typescript
import { test, expect } from "@playwright/test";

// Page Object Model
class LoginPage {
  constructor(private page: Page) {}
  
  async goto() { await this.page.goto("/login"); }
  async login(email: string, password: string) {
    await this.page.fill('[data-testid="email"]', email);
    await this.page.fill('[data-testid="password"]', password);
    await this.page.click('[data-testid="submit"]');
    await this.page.waitForURL("/dashboard");
  }
}

test("user can login and see dashboard", async ({ page }) => {
  const login = new LoginPage(page);
  await login.goto();
  await login.login("test@example.com", "password123");
  
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Welcome, Test User")).toBeVisible();
});

// Visual regression
test("homepage matches snapshot", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveScreenshot("homepage.png", { maxDiffPixels: 100 });
});
```

## Performance Testing

```javascript
// k6 load test
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  stages: [
    { duration: "30s", target: 50 },   // ramp up
    { duration: "2m", target: 50 },    // steady
    { duration: "30s", target: 200 },  // spike
    { duration: "1m", target: 0 },     // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95) < 500", "p(99) < 1000"],
    http_req_failed: ["rate < 0.01"],
  },
};

export default function () {
  const res = http.get("https://api.example.com/users");
  check(res, {
    "status is 200": (r) => r.status === 200,
    "response time < 500ms": (r) => r.timings.duration < 500,
  });
  sleep(1);
}
```

### Security Testing Checklist
- **SAST**: Semgrep, CodeQL — scan source code for vulnerabilities
- **DAST**: OWASP ZAP — scan running app for vulnerabilities
- **SCA**: Snyk, npm audit — scan dependencies for CVEs
- **Secret scanning**: TruffleHog, gitleaks — detect leaked secrets in git history
- **Container scanning**: Trivy — scan Docker images for CVEs

---

# 7. Security (Application Security)

## OWASP Top 10 (2021+) — Quick Reference

| # | Risk | Prevention |
|---|---|---|
| 1 | **Broken Access Control** | Deny by default, RBAC/ABAC, validate ownership on every request |
| 2 | **Cryptographic Failures** | TLS 1.3, AES-256-GCM, bcrypt/argon2, no MD5/SHA1 for passwords |
| 3 | **Injection** | Parameterized queries, ORM, input validation, CSP |
| 4 | **Insecure Design** | Threat modeling, abuse cases, defense in depth |
| 5 | **Security Misconfiguration** | Hardened defaults, no default creds, minimal permissions |
| 6 | **Vulnerable Components** | SCA scanning, lock files, automatic updates, SBOM |
| 7 | **Auth Failures** | MFA, rate limiting, secure session, no cred stuffing |
| 8 | **Data Integrity Failures** | Verify signatures, signed artifacts, CI/CD security |
| 9 | **Logging Failures** | Audit logs for auth/authz, tamper-proof, alerting |
| 10 | **SSRF** | Allowlist URLs, disable redirects, network segmentation |

## Authentication Patterns

### OAuth 2.0 + PKCE (SPA / Mobile)
```
1. Client generates code_verifier (random) + code_challenge (SHA256 hash)
2. Client redirects to /authorize?response_type=code&code_challenge=...&code_challenge_method=S256
3. User authenticates, consents
4. Auth server redirects back with authorization code
5. Client exchanges code + code_verifier for tokens at /token
6. Auth server verifies SHA256(code_verifier) === code_challenge
7. Returns access_token (short-lived) + refresh_token (long-lived)
```

### JWT Best Practices
```typescript
// Access token: short-lived (15 min), stored in memory only
// Refresh token: long-lived (7-30 days), httpOnly secure cookie, rotate on use
// ID token: user info, never send to your own API

const accessToken = jwt.sign(
  { sub: user.id, role: user.role, iss: "api.example.com" },
  ACCESS_SECRET,
  { expiresIn: "15m", algorithm: "RS256" },  // RS256 for asymmetric (public key verification)
);

// Middleware — verify + extract
function authenticate(req: Request): AuthContext {
  const token = req.headers.get("Authorization")?.replace("Bearer ", "");
  if (!token) throw new UnauthorizedError("missing token");
  try {
    const payload = jwt.verify(token, PUBLIC_KEY, { algorithms: ["RS256"], issuer: "api.example.com" });
    return { userId: payload.sub, role: payload.role };
  } catch (e) {
    if (e instanceof jwt.TokenExpiredError) throw new UnauthorizedError("token expired");
    throw new UnauthorizedError("invalid token");
  }
}
```

### Session Management
```typescript
// Server-side sessions (more secure than JWT for web apps)
// Session ID in httpOnly, secure, sameSite=strict cookie
// Session data in Redis (auto-expire)

app.use(session({
  store: new RedisStore({ client: redis, prefix: "sess:" }),
  secret: process.env.SESSION_SECRET,
  name: "__session",
  cookie: {
    httpOnly: true,      // no JS access
    secure: true,        // HTTPS only
    sameSite: "strict",  // CSRF protection
    maxAge: 24 * 60 * 60 * 1000,  // 24 hours
  },
  resave: false,
  saveUninitialized: false,
}));
```

## Authorization Models

| Model | When | How |
|---|---|---|
| **RBAC** | Simple role hierarchy | `user.role === "admin"` |
| **ABAC** | Complex attribute-based rules | Policy engine evaluates attributes |
| **ReBAC** | Relationship-based (Google Zanzibar) | `user X is member of org Y which owns resource Z` |

```typescript
// RBAC — never hardcode role checks, use permissions
const PERMISSIONS = {
  admin: ["users:read", "users:write", "orders:read", "orders:write", "billing:manage"],
  manager: ["users:read", "orders:read", "orders:write"],
  member: ["orders:read"],
} as const;

function authorize(user: AuthContext, permission: string): void {
  const perms = PERMISSIONS[user.role] ?? [];
  if (!perms.includes(permission)) throw new ForbiddenError(`missing permission: ${permission}`);
}

// Always check resource ownership too
async function getOrder(user: AuthContext, orderId: string): Promise<Order> {
  const order = await orderRepo.findById(orderId);
  if (!order) throw new NotFoundError("order");
  if (user.role !== "admin" && order.userId !== user.userId) throw new ForbiddenError("not your order");
  return order;
}
```

## Encryption

| Use Case | Algorithm | Notes |
|---|---|---|
| Passwords | **Argon2id** (preferred) or bcrypt | Never SHA/MD5. Salt is automatic. |
| Data at rest | **AES-256-GCM** | Authenticated encryption. Unique nonce per message. |
| Data in transit | **TLS 1.3** | Disable TLS 1.0/1.1. HSTS header. |
| Signing | **Ed25519** or RSA-PSS | For JWT, webhooks, artifacts. |
| Hashing (non-password) | **SHA-256** / BLAKE3 | For checksums, dedup, HMAC. |

## Input Validation

```typescript
// Allowlist validation at every boundary
// Never trust: URL params, headers, body, cookies, file uploads

// SQL injection prevention — ALWAYS parameterized
const user = await db.query("SELECT * FROM users WHERE email = $1", [email]);  // SAFE
// const user = await db.query(`SELECT * FROM users WHERE email = '${email}'`); // VULNERABLE

// XSS prevention — encode output by context
// HTML: escape < > & " '  (React does this by default)
// URL: encodeURIComponent()
// JS: JSON.stringify() into script context
// CSS: whitelist values only

// CSRF — SameSite cookies + CSRF token for non-GET requests
// CSP header
const cspHeader = [
  "default-src 'self'",
  "script-src 'self' 'nonce-{random}'",  // nonce per request
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: https:",
  "connect-src 'self' https://api.example.com",
  "frame-ancestors 'none'",
  "base-uri 'self'",
].join("; ");

// Security headers
{
  "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
```

## Secrets Management
- **Never** in source code, environment files committed to git, or Docker images
- Use: AWS Secrets Manager, SSM Parameter Store, HashiCorp Vault, Doppler
- Rotate secrets automatically. Detect leaked secrets with gitleaks/TruffleHog in CI.
- API keys: hash before storage, prefix for identification (`sk_live_`, `pk_test_`)
- Use short-lived credentials (IAM roles, OIDC federation) over long-lived API keys

---

# 8. Performance Engineering

## Profiling — Measure First

| Language | CPU | Memory | Tool |
|---|---|---|---|
| Node.js | `--prof`, `--inspect` | `--inspect` heap snapshot | Chrome DevTools, Clinic.js |
| Go | `pprof` | `pprof` | `go tool pprof`, Pyroscope |
| Python | `py-spy` | `tracemalloc` | py-spy, Scalene |
| Rust | `perf`, `flamegraph` | `valgrind` | cargo-flamegraph |
| Browser | Lighthouse, DevTools | DevTools Memory | Performance tab |

### Flame Graph Reading
- **Wide bars** = function spending lots of time (hot path)
- **Tall stacks** = deep call chains
- **Flat tops** = leaf functions doing actual work
- Look for: unexpected wide bars, recursive patterns, synchronous I/O

## Database Performance

### Query Optimization Checklist
1. Run `EXPLAIN (ANALYZE, BUFFERS)` on slow queries
2. Look for: Seq Scan on large tables, high actual vs estimated rows, nested loops with large outer
3. Add indexes matching your WHERE, JOIN, ORDER BY
4. Use `LIMIT` + cursor pagination (not `OFFSET`)
5. Avoid `SELECT *` — select only needed columns
6. Batch operations: `INSERT ... VALUES (), (), ()` not individual inserts
7. Use connection pooling (PgBouncer, RDS Proxy)

### N+1 Prevention
```typescript
// BAD: N+1 (1 query for users + N queries for orders)
const users = await db.query("SELECT * FROM users LIMIT 20");
for (const user of users) {
  user.orders = await db.query("SELECT * FROM orders WHERE user_id = $1", [user.id]);
}

// GOOD: 2 queries total (batch load)
const users = await db.query("SELECT * FROM users LIMIT 20");
const userIds = users.map(u => u.id);
const orders = await db.query("SELECT * FROM orders WHERE user_id = ANY($1)", [userIds]);
const ordersByUser = groupBy(orders, "user_id");
users.forEach(u => { u.orders = ordersByUser[u.id] ?? []; });

// GOOD: Single query with JOIN (when appropriate)
const result = await db.query(`
  SELECT u.*, json_agg(o.*) AS orders
  FROM users u LEFT JOIN orders o ON o.user_id = u.id
  GROUP BY u.id LIMIT 20
`);
```

## Caching Hierarchy

```
Request → L1 (in-process Map/LRU, <1ms)
        → L2 (Redis/Memcached, 1-5ms)
        → L3 (CDN edge, 10-50ms)
        → Origin (DB/API, 50-500ms)
```

### Cache Invalidation Strategies
| Strategy | How | When |
|---|---|---|
| **TTL** | Auto-expire after N seconds | Tolerance for staleness |
| **Event-driven** | Invalidate on write event | Strong consistency needed |
| **Write-through** | Update cache on every write | Read-heavy, predictable writes |
| **Cache-aside + short TTL** | Lazy populate, expire quickly | Good default |
| **Versioned keys** | `user:123:v5` | Atomic cache busting |

### HTTP Caching
```typescript
// Immutable assets (JS, CSS with hash in filename)
"Cache-Control": "public, max-age=31536000, immutable"

// API responses (private, revalidate)
"Cache-Control": "private, no-cache"  // always revalidate with ETag
"ETag": "W/\"abc123\""

// Static pages (CDN cacheable, revalidate after 60s)
"Cache-Control": "public, s-maxage=60, stale-while-revalidate=300"

// Never cache (auth endpoints, user-specific data without ETag)
"Cache-Control": "no-store"
```

## Backend Performance

### Connection Pooling
```typescript
// PostgreSQL — pool with PgBouncer or built-in
const pool = new Pool({
  max: 20,                    // max connections (match CPU cores * 2-3)
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 5000,
});

// HTTP keep-alive (reuse TCP connections)
const agent = new Agent({ keepAlive: true, maxSockets: 50 });

// Redis connection pooling via ioredis cluster/sentinel
```

### Batch Processing
```typescript
// Process in batches to avoid memory exhaustion
async function processAllUsers(batchSize = 1000) {
  let cursor = undefined;
  while (true) {
    const batch = await db.query(
      "SELECT * FROM users WHERE id > $1 ORDER BY id LIMIT $2",
      [cursor ?? "", batchSize],
    );
    if (batch.length === 0) break;
    
    await Promise.all(batch.map(user => processUser(user)));
    cursor = batch.at(-1)!.id;
  }
}
```

### Streaming (avoid loading large datasets into memory)
```typescript
// Stream large CSV export
app.get("/export/orders", async (req, res) => {
  res.setHeader("Content-Type", "text/csv");
  res.setHeader("Content-Disposition", "attachment; filename=orders.csv");
  res.write("id,user_id,total,created_at\n");
  
  const cursor = db.query(new Cursor("SELECT * FROM orders ORDER BY id"));
  while (true) {
    const rows = await cursor.read(1000);
    if (rows.length === 0) break;
    for (const row of rows) {
      res.write(`${row.id},${row.user_id},${row.total},${row.created_at}\n`);
    }
  }
  res.end();
});
```

---

# 9. Data Engineering & AI/ML Integration

## ETL/ELT Patterns

| Pattern | When | Tools |
|---|---|---|
| **ETL** (Extract-Transform-Load) | Transform before loading, schema enforcement | Airflow, Prefect, custom |
| **ELT** (Extract-Load-Transform) | Load raw, transform in warehouse | dbt, Snowflake, BigQuery |
| **Streaming** | Real-time processing | Kafka + Flink/Spark Streaming |
| **Change Data Capture** | Replicate DB changes | Debezium, DynamoDB Streams |

## Vector Databases & Embeddings

| DB | Hosted | Self-hosted | Notes |
|---|---|---|---|
| **pgvector** | Any PG host | Yes | Add to existing PG. Good for <10M vectors |
| **Qdrant** | Cloud | Yes (Rust) | High performance, filtering |
| **Pinecone** | Managed only | No | Fully managed, serverless |
| **Chroma** | No | Yes (Python) | Simple, good for prototyping |
| **Weaviate** | Cloud | Yes | Hybrid search (vector + keyword) |

```sql
-- pgvector setup
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content TEXT NOT NULL,
  embedding vector(1536),  -- OpenAI ada-002 dimension
  metadata JSONB DEFAULT '{}'
);

CREATE INDEX ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
-- Or HNSW for better recall:
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Similarity search
SELECT id, content, 1 - (embedding <=> $1::vector) AS similarity
FROM documents
WHERE metadata->>'category' = 'technical'
ORDER BY embedding <=> $1::vector
LIMIT 10;
```

## RAG (Retrieval-Augmented Generation)

```
User Query → Embed Query → Vector Search → Retrieve Chunks → Rerank → Build Prompt → LLM → Response
```

### Chunking Strategies
| Strategy | When | Chunk Size |
|---|---|---|
| **Fixed size** | Simple, predictable | 500-1000 tokens with 50-100 overlap |
| **Sentence-based** | Natural boundaries | 3-5 sentences |
| **Semantic** | Best quality, higher cost | Split on topic shifts (embedding similarity) |
| **Recursive** | Structured docs | Split by headers, then paragraphs, then sentences |
| **Document-specific** | Code, markdown, HTML | Language-aware parsers (tree-sitter for code) |

```typescript
// RAG pipeline
async function ragQuery(question: string): Promise<string> {
  // 1. Embed the question
  const queryEmbedding = await openai.embeddings.create({
    model: "text-embedding-3-small",
    input: question,
  });

  // 2. Retrieve relevant chunks
  const chunks = await db.query(`
    SELECT content, metadata, 1 - (embedding <=> $1::vector) AS score
    FROM documents
    ORDER BY embedding <=> $1::vector
    LIMIT 10
  `, [queryEmbedding.data[0].embedding]);

  // 3. Rerank (optional, improves quality significantly)
  const reranked = await cohere.rerank({
    query: question,
    documents: chunks.map(c => c.content),
    topN: 5,
  });

  // 4. Build prompt with context
  const context = reranked.map(r => chunks[r.index].content).join("\n---\n");
  
  // 5. Generate answer
  const response = await openai.chat.completions.create({
    model: "gpt-4o",
    messages: [
      { role: "system", content: `Answer based on the provided context. If the context doesn't contain the answer, say so.\n\nContext:\n${context}` },
      { role: "user", content: question },
    ],
  });

  return response.choices[0].message.content;
}
```

## LLM Integration Patterns

### Structured Output
```typescript
// Force JSON output with schema
const response = await openai.chat.completions.create({
  model: "gpt-4o",
  response_format: {
    type: "json_schema",
    json_schema: {
      name: "analysis",
      schema: {
        type: "object",
        properties: {
          sentiment: { type: "string", enum: ["positive", "negative", "neutral"] },
          confidence: { type: "number", minimum: 0, maximum: 1 },
          topics: { type: "array", items: { type: "string" } },
        },
        required: ["sentiment", "confidence", "topics"],
      },
    },
  },
  messages: [{ role: "user", content: `Analyze: ${text}` }],
});

// Validate with Zod after parsing
const AnalysisSchema = z.object({
  sentiment: z.enum(["positive", "negative", "neutral"]),
  confidence: z.number().min(0).max(1),
  topics: z.array(z.string()),
});
const analysis = AnalysisSchema.parse(JSON.parse(response.choices[0].message.content));
```

### Function Calling / Tool Use
```typescript
const tools = [{
  type: "function",
  function: {
    name: "search_orders",
    description: "Search orders by customer email or order ID",
    parameters: {
      type: "object",
      properties: {
        email: { type: "string", description: "Customer email" },
        orderId: { type: "string", description: "Order ID" },
        status: { type: "string", enum: ["pending", "shipped", "delivered"] },
      },
    },
  },
}];

// Handle tool calls in a loop
let messages = [{ role: "user", content: userMessage }];
while (true) {
  const response = await openai.chat.completions.create({ model: "gpt-4o", messages, tools });
  const msg = response.choices[0].message;
  messages.push(msg);
  
  if (msg.tool_calls) {
    for (const call of msg.tool_calls) {
      const result = await executeTool(call.function.name, JSON.parse(call.function.arguments));
      messages.push({ role: "tool", tool_call_id: call.id, content: JSON.stringify(result) });
    }
  } else {
    return msg.content;  // final answer
  }
}
```

### Streaming
```typescript
// Stream LLM response to client
const stream = await openai.chat.completions.create({
  model: "gpt-4o",
  messages,
  stream: true,
});

// Server-Sent Events to client
res.setHeader("Content-Type", "text/event-stream");
for await (const chunk of stream) {
  const content = chunk.choices[0]?.delta?.content;
  if (content) res.write(`data: ${JSON.stringify({ content })}\n\n`);
}
res.write("data: [DONE]\n\n");
res.end();
```

---

# 10. Solution Architecture

## System Design Primitives

### Load Balancing
| Algorithm | When |
|---|---|
| **Round Robin** | Homogeneous instances, stateless |
| **Least Connections** | Variable request duration |
| **Consistent Hashing** | Sticky sessions, cache locality |
| **Weighted** | Mixed instance sizes |

### Consistency Models
```
Strong ────────────────────────────── Eventual
  │                                      │
  Linearizable  Sequential  Causal  Eventual
  (single leader) (total order) (happens-before) (converge)
  
  CAP: Choose 2 of {Consistency, Availability, Partition tolerance}
  PACELC: If Partition → {A, C}; Else → {Latency, Consistency}
```

| System | Model | Trade-off |
|---|---|---|
| PostgreSQL (single) | Linearizable | Low availability on failure |
| DynamoDB | Eventual (default), Strong (per-request) | Higher latency for strong |
| Cassandra | Tunable (quorum) | Tune per query |
| Redis | Eventual (replication) | Data loss on leader failure |

### Sharding Strategies
| Strategy | How | When |
|---|---|---|
| **Range** | Shard by ID range | Sequential access patterns |
| **Hash** | Hash key mod N shards | Uniform distribution |
| **Geographic** | Shard by region | Data locality requirements |
| **Tenant** | Shard by tenant ID | Multi-tenant SaaS |

### Replication
- **Leader-Follower**: Writes to leader, reads from followers. Simple, read-scalable.
- **Multi-Leader**: Writes to any leader. Conflict resolution needed. Geographic distribution.
- **Leaderless** (Dynamo-style): Quorum reads/writes. High availability.

## Scalability Patterns

```
Vertical Scaling: Bigger machine (limited)
Horizontal Scaling: More machines (preferred)

Stateless services → horizontal scale trivially
Stateful services → need sharding, replication, or externalized state (Redis/DB)
```

### Auto-scaling Signals
| Signal | When |
|---|---|
| CPU > 70% | Compute-bound workloads |
| Request queue depth | IO-bound workloads |
| Custom metric (business) | Orders/sec, concurrent users |
| Scheduled | Known traffic patterns |

## Reliability

### Graceful Degradation Cascade
```
1. Retry (with backoff + jitter)
   ↓ fails
2. Circuit breaker opens → use fallback
   ↓ fallback fails
3. Degrade (show cached/stale data, disable non-critical features)
   ↓ still failing
4. Fail-fast with clear error message
```

### Graceful Shutdown
```typescript
// 1. Stop accepting new requests
// 2. Drain in-flight requests (with timeout)
// 3. Close connections (DB, Redis, queues)
// 4. Exit

const server = app.listen(3000);
const shutdown = async (signal: string) => {
  log.info(`${signal} received, starting graceful shutdown`);
  
  // Stop accepting new connections
  server.close();
  
  // Wait for in-flight requests (max 30s)
  await Promise.race([
    drainConnections(),
    new Promise(r => setTimeout(r, 30_000)),
  ]);
  
  // Close external connections
  await Promise.allSettled([
    db.end(),
    redis.quit(),
    kafka.disconnect(),
  ]);
  
  process.exit(0);
};

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
```

### Chaos Engineering Principles
1. Define steady state (baseline metrics)
2. Hypothesize what happens when X fails
3. Inject failure (kill instance, add latency, corrupt data)
4. Observe deviation from steady state
5. Fix weaknesses found

Tools: AWS FIS, Chaos Monkey, LitmusChaos, Gremlin

## Observability

### Structured Logging
```typescript
// Always structured JSON, never string interpolation
const log = pino({
  level: process.env.LOG_LEVEL ?? "info",
  formatters: {
    level: (label) => ({ level: label }),
  },
  base: { service: "order-api", version: process.env.APP_VERSION },
});

// Add request context via async local storage
const requestContext = new AsyncLocalStorage<{ requestId: string; userId?: string }>();

function logWithContext(level: string, msg: string, data?: object) {
  const ctx = requestContext.getStore();
  log[level]({ ...ctx, ...data }, msg);
}

// Middleware sets context
app.use((req, res, next) => {
  const requestId = req.headers["x-request-id"] ?? randomUUID();
  res.setHeader("x-request-id", requestId);
  requestContext.run({ requestId }, next);
});

// Usage
logWithContext("info", "order placed", { orderId: "123", total: 99.99 });
// Output: {"level":"info","service":"order-api","requestId":"abc","msg":"order placed","orderId":"123","total":99.99}
```

### Distributed Tracing (OpenTelemetry)
```typescript
import { trace, SpanStatusCode } from "@opentelemetry/api";

const tracer = trace.getTracer("order-service");

async function placeOrder(req: PlaceOrderRequest): Promise<Order> {
  return tracer.startActiveSpan("placeOrder", async (span) => {
    try {
      span.setAttribute("order.userId", req.userId);
      span.setAttribute("order.itemCount", req.items.length);
      
      const order = await createOrder(req);
      span.setAttribute("order.id", order.id);
      span.setAttribute("order.total", order.total);
      
      return order;
    } catch (error) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      span.recordException(error);
      throw error;
    } finally {
      span.end();
    }
  });
}
```

### Metrics — RED + USE

| Method | Metric | For |
|---|---|---|
| **RED** (services) | **R**ate, **E**rrors, **D**uration | Request-driven services |
| **USE** (resources) | **U**tilization, **S**aturation, **E**rrors | CPU, memory, disk, network |

```typescript
// Prometheus metrics example
const httpRequestDuration = new Histogram({
  name: "http_request_duration_seconds",
  help: "HTTP request duration in seconds",
  labelNames: ["method", "route", "status"],
  buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5],
});

const httpRequestsTotal = new Counter({
  name: "http_requests_total",
  help: "Total HTTP requests",
  labelNames: ["method", "route", "status"],
});

// Middleware
app.use((req, res, next) => {
  const end = httpRequestDuration.startTimer({ method: req.method, route: req.route?.path ?? "unknown" });
  res.on("finish", () => {
    const labels = { method: req.method, route: req.route?.path ?? "unknown", status: res.statusCode };
    end(labels);
    httpRequestsTotal.inc(labels);
  });
  next();
});
```

### Alerting Rules
- Alert on **symptoms** (error rate > 1%, p99 latency > 2s), not causes (CPU > 80%)
- Use severity levels: **page** (customer impact now), **ticket** (degradation, fix in hours), **log** (investigate when convenient)
- Include runbook link in every alert
- Alert on absence of expected events (dead-man switch)

---

# 11. Developer Experience

## Git Workflow

### Conventional Commits
```
<type>(<scope>): <description>

feat(auth): add PKCE support for OAuth2 flow
fix(orders): prevent duplicate charges on retry
refactor(db): extract connection pool configuration
perf(api): add Redis cache for user profile endpoint
docs(api): update OpenAPI spec for v2 endpoints
test(orders): add integration tests for refund flow
chore(deps): bump fastify from 4.x to 5.x
ci(deploy): add staging environment to pipeline
```

### Trunk-Based Development
- `main` is always deployable. Short-lived feature branches (<2 days).
- Feature flags for incomplete features in main.
- No long-lived branches. No release branches (tag instead).
- CI runs on every push. Merge only if green.

### Feature Flags
```typescript
// Evaluate once at entry point, pass boolean down
const flags = await featureFlagService.evaluate(user);

if (flags.newCheckoutFlow) {
  return <NewCheckout />;
} else {
  return <LegacyCheckout />;
}

// Flag lifecycle: create → enable for team → % rollout → 100% → remove flag + old code
```

## Code Review Checklist

### What to Look For
- [ ] Correctness: Does it do what the ticket says?
- [ ] Edge cases: Null, empty, max values, concurrent access
- [ ] Security: Input validation, auth checks, no secrets in code
- [ ] Performance: N+1 queries, unbounded lists, missing indexes
- [ ] Error handling: Are errors caught, logged, and surfaced correctly?
- [ ] Tests: Are the right things tested? Are tests reliable?
- [ ] Naming: Can I understand the code without comments?
- [ ] Simplicity: Is there a simpler way to do this?

### PR Size
- Ideal: < 200 lines changed. Max: 400 lines.
- If larger: split into stacked PRs (feature flag the incomplete parts).
- One concern per PR: don't mix refactoring with features.

## Monorepo Tooling

| Tool | Strengths | When |
|---|---|---|
| **Turborepo** | Simple, fast, Vercel ecosystem | Next.js projects, simple monorepo |
| **Nx** | Powerful, plugin ecosystem, affected analysis | Large monorepo, multiple frameworks |
| **pnpm workspaces** | Fast installs, strict deps | Any JS/TS monorepo |

```jsonc
// turbo.json
{
  "tasks": {
    "build": { "dependsOn": ["^build"], "outputs": ["dist/**", ".next/**"] },
    "test": { "dependsOn": ["build"] },
    "lint": {},
    "dev": { "cache": false, "persistent": true }
  }
}
```

## Documentation

### Architecture Decision Records (ADRs)
```markdown
# ADR-001: Use PostgreSQL for primary data store

## Status: Accepted
## Date: 2024-03-15

## Context
We need a primary data store for the order management system. Requirements:
- ACID transactions for financial data
- Complex queries (joins, aggregation, full-text search)
- JSON support for flexible metadata

## Decision
PostgreSQL 16 with pgvector extension for future AI features.

## Consequences
- (+) Strong consistency, mature ecosystem, great tooling
- (+) pgvector eliminates need for separate vector DB initially
- (-) Vertical scaling limits (mitigated by read replicas + connection pooling)
- (-) Team needs PostgreSQL-specific knowledge (CTEs, JSONB, etc.)
```

---

# 12. Engineering Principles

## Code Structure Rules
- Guard clauses at top, happy path flows down. No nesting >3 levels.
- Feature-based modules, colocated tests. Barrel exports for public API.
- Vertical slices per feature (handler -> service -> repository).
- Hexagonal: core logic depends on nothing. Adapters connect externals.

## Naming Conventions

| Type | Convention | Example |
|---|---|---|
| Sync accessor | `get` | `getUser()`, `getName()` |
| Async accessor | `fetch` | `fetchOrders()`, `fetchProfile()` |
| Derived value | `compute` | `computeTotal()`, `computeDiscount()` |
| Idempotent create | `ensure` | `ensureBucket()`, `ensureIndex()` |
| Parse/extract | `parse` | `parseEmail()`, `parseConfig()` |
| Boolean | `is/has/can/should` | `isActive`, `hasPermission`, `canRetry` |
| Collections | Plural noun | `users`, `orders` |
| Maps | `by` + key | `usersByEmail`, `pricesByCurrency` |
| Units | Include unit | `retryDelayMs`, `maxFileSizeBytes` |

## Error Handling Pattern
```typescript
// Result type — prefer over throwing
type Result<T, E = Error> = { ok: true; data: T } | { ok: false; error: E };

function Ok<T>(data: T): Result<T, never> { return { ok: true, data }; }
function Err<E>(error: E): Result<never, E> { return { ok: false, error }; }

// Domain errors with context
class OrderError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly context: Record<string, unknown>,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "OrderError";
  }
}

// Usage
async function chargeOrder(orderId: string): Promise<Result<Receipt, OrderError>> {
  const order = await orders.findById(orderId);
  if (!order) return Err(new OrderError("not found", "ORDER_NOT_FOUND", { orderId }));
  
  const charge = await payments.charge(order.total);
  if (!charge.ok) {
    return Err(new OrderError("payment failed", "PAYMENT_FAILED", { orderId, amount: order.total }, { cause: charge.error }));
  }
  
  return Ok({ orderId, transactionId: charge.data.id, amount: order.total });
}
```

## Data-Driven Logic
```typescript
// Table-driven instead of if/else chains
const PRICING_TIERS = {
  free:       { maxUsers: 5,    maxStorage: "1GB",   price: 0 },
  starter:    { maxUsers: 20,   maxStorage: "10GB",  price: 29 },
  pro:        { maxUsers: 100,  maxStorage: "100GB", price: 99 },
  enterprise: { maxUsers: Infinity, maxStorage: "unlimited", price: 499 },
} as const;

type Tier = keyof typeof PRICING_TIERS;

function getLimits(tier: Tier) { return PRICING_TIERS[tier]; }
function isOverLimit(tier: Tier, userCount: number) { return userCount > PRICING_TIERS[tier].maxUsers; }

// Config hierarchy: defaults -> config -> env -> runtime
const config = {
  port: Number(process.env.PORT) ?? configFile.port ?? 3000,
  dbUrl: process.env.DATABASE_URL ?? configFile.dbUrl ?? "postgres://localhost/app",
  logLevel: process.env.LOG_LEVEL ?? configFile.logLevel ?? "info",
};
```

## Idempotency
```typescript
// Idempotency key for mutation endpoints
app.post("/orders", async (req, res) => {
  const idempotencyKey = req.headers["idempotency-key"];
  if (!idempotencyKey) return res.status(400).json({ error: "idempotency-key header required" });

  // Check if already processed
  const existing = await redis.get(`idem:${idempotencyKey}`);
  if (existing) return res.status(200).json(JSON.parse(existing));

  // Process (with lock to prevent concurrent duplicates)
  const lock = await redis.set(`idem-lock:${idempotencyKey}`, "1", "NX", "EX", 30);
  if (!lock) return res.status(409).json({ error: "request in progress" });

  try {
    const order = await orderService.create(req.body);
    await redis.set(`idem:${idempotencyKey}`, JSON.stringify(order), "EX", 86400);
    return res.status(201).json(order);
  } finally {
    await redis.del(`idem-lock:${idempotencyKey}`);
  }
});

// Database-level: upsert
INSERT INTO orders (id, user_id, total)
VALUES ($1, $2, $3)
ON CONFLICT (id) DO NOTHING
RETURNING *;
```

## Concurrency Patterns

```typescript
// Semaphore — limit concurrent operations
class Semaphore {
  private queue: (() => void)[] = [];
  private active = 0;
  constructor(private max: number) {}

  async acquire(): Promise<void> {
    if (this.active < this.max) { this.active++; return; }
    await new Promise<void>(resolve => this.queue.push(resolve));
  }

  release(): void {
    this.active--;
    const next = this.queue.shift();
    if (next) { this.active++; next(); }
  }

  async run<T>(fn: () => Promise<T>): Promise<T> {
    await this.acquire();
    try { return await fn(); }
    finally { this.release(); }
  }
}

// Process 100 items, max 10 concurrent
const sem = new Semaphore(10);
await Promise.all(items.map(item => sem.run(() => processItem(item))));
```

---

# 13. Anti-Patterns Reference

| Anti-pattern | Why Bad | Fix |
|---|---|---|
| God object/service | Everything depends on it, untestable | Split by domain responsibility |
| Premature optimization | Wastes time, adds complexity | Measure first, optimize hot paths only |
| Shared mutable state | Race conditions, hard to reason | Immutable data, message passing |
| Stringly-typed | No compile-time safety | Enums, branded types, discriminated unions |
| Boolean blindness | `process(true, false, true)` | Named params, enums, option objects |
| Cargo cult architecture | Microservices for 2-person team | Match architecture to team/problem size |
| Over-abstraction | 5 layers for simple CRUD | YAGNI — add abstraction when needed, not before |
| Silent failure | `catch (e) {}` | Log, rethrow, or return error. Never swallow. |
| N+1 queries | O(N) queries instead of O(1) | DataLoader, JOINs, batch queries |
| Distributed monolith | Microservices with tight coupling | If everything deploys together, it's a monolith with network overhead |
| Token in localStorage | XSS can steal tokens | httpOnly secure cookies for refresh tokens |
| `SELECT *` | Fetches unused columns, breaks on schema change | Select specific columns |
| Offset pagination | Slow on large tables, unstable pages | Cursor-based pagination |
| Synchronous email/SMS | Blocks request, fragile | Queue + async worker |
| Hardcoded config | Different per environment | Env vars / config service |

---

# Quick Reference — Decision Matrices

## When to Use What Database

| Need | Choose | Avoid |
|---|---|---|
| ACID transactions, complex queries | **PostgreSQL** | MongoDB |
| Key-value, sub-ms latency | **Redis** | PostgreSQL |
| Document store, flexible schema | **MongoDB** | When you need JOINs |
| Massive write throughput, auto-scale | **DynamoDB** | Complex queries, JOINs |
| Full-text search | **PostgreSQL** (built-in) or **Elasticsearch** | Rolling your own |
| Time series | **TimescaleDB** or **InfluxDB** | Generic RDBMS |
| Graph relationships | **Neo4j** or **Neptune** | Recursive SQL CTEs at scale |
| Vector similarity | **pgvector** (<10M) or **Qdrant** (>10M) | LIKE queries on embeddings |

## When to Use What Communication Pattern

| Need | Use |
|---|---|
| Request-response, immediate result | REST / gRPC |
| Real-time bidirectional | WebSocket |
| Server push, one-way | SSE (Server-Sent Events) |
| Async processing, decoupled | Message queue (SQS, Kafka) |
| Cross-service events | Event bus (EventBridge, SNS) |
| Complex workflows | Orchestration (Step Functions) |
| End-to-end type safety (full-stack TS) | tRPC |
| Mobile/web with flexible queries | GraphQL |

## When to Use What Auth

| Scenario | Use |
|---|---|
| SPA / Mobile app | OAuth 2.0 + PKCE |
| Server-to-server | Client Credentials + mTLS |
| SSR web app | Session cookies (httpOnly, secure, sameSite) |
| API consumers | API keys (hashed, prefixed, scoped) |
| Microservice-to-microservice | JWT with short TTL or mTLS |
| Internal tools | SSO via OIDC (Okta, Auth0, Cognito) |

---

# Safety Checklist (Every PR)

- [ ] Input validated at boundary (Zod/Pydantic, allowlist)
- [ ] Parameterized queries (no string interpolation in SQL)
- [ ] Auth check on every endpoint (default deny)
- [ ] Resource ownership verified (not just role check)
- [ ] Secrets not in code (env vars / secrets manager)
- [ ] Error responses don't leak internals
- [ ] Rate limiting on public endpoints
- [ ] Pagination on all list endpoints (cursor-based)
- [ ] Logs don't contain PII/secrets
- [ ] Dependencies scanned for CVEs

---

# 14. Project-Specific Knowledge (MUST READ)

These files contain our ACTUAL codebases, APIs, and services. This dev-skills.md has general patterns — the files below have our specific code structure.

## Architecture & Teams
- **`knowledge/architecture.md`** — Agent team roles, when to use swarm vs direct, decision framework, code review checklist

## Excise Services (Our Main Product)
- **`knowledge/excise-services.md`** — All service internals:
  - **wine-nodejs-api**: TypeScript, Express 5.1, MSSQL/Sequelize, API v2-v6, JWT auth, Winston logging, Algolia/Typesense search
  - **wine-go-api**: Go 1.24, Gorilla Mux, Firestore, Cognito, pkg/ structure (log, middleware, db, cognito, restful, rmdb)
  - **wine-authen**: Lambda, Firebase->Cognito migration trigger (tbit-exciseft + tbit-excise)
  - **wine-proxy**: Nginx reverse proxy, SSL/TLS, upstream to winefasttrack.excise.go.th
  - **wine-frontend**: React/Vite | **wine-fasttrack-mobile**: Flutter/Fastlane
  - **car-backend**: Express 4.21, Prisma 5.22/MSSQL, Zod validation, Swagger UI, v2.2.22
  - **car-cron**: Lambda, Cheerio scraping Thai Customs for currency rates
  - **elephant**: Car dev variant with latest deps (Express 5.2, Prisma 7.2)

## Axentx Projects
- **`knowledge/axentx-projects.md`** — Side projects with different stacks:
  - **Costinel**: React 18/Vite/TailwindCSS/Recharts + Supabase/PostgREST/Kong/Redis
  - **Vanguard**: FastAPI/SQLAlchemy/AsyncPG + React/Zustand + Neo4j/Celery/MinIO
  - **AxiomOps**: Turbo monorepo (pnpm), Node.js/TypeScript, 9 microservices, React/TanStack Query
  - **Arkship**: FastAPI + React/Vite, Temporal workflows, 130+ Python modules
  - **Surrogate-1**: Python, Transformers/PEFT/bitsandbytes, LangGraph agents
  - **Workio**: React/Express/TypeScript + LINE Messaging API + PostgreSQL

## Workspace Map
- **`knowledge/workspace-map.md`** — Quick lookup for EVERY project path

---

## Related Patterns (Graph Links)

- [[../patterns/engineering/naming-conventions]] · [[../patterns/engineering/comments-only-when-needed]] · [[../patterns/engineering/right-sized-no-overengineer]] · [[../patterns/engineering/codebase-first]]
- [[../patterns/process/silent-execution]] · [[../patterns/process/never-abandon-tasks]]
- [[../patterns/skills/auto-activation]] · [[../patterns/skills/awesome-list-vs-actual-skill]]
- [[../patterns/MOC|🧭 Knowledge Graph Hub]]

> This file is loaded into every AI session as the universal dev reference.
> Cross-reference with project-specific knowledge files above for our actual code.
> Last updated: 2026-04-16
> Location: ~/Documents/Obsidian Vault/AI-Hub/knowledge/dev-skills.md
