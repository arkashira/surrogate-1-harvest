# workio / discovery

## Final Synthesis — Best parts merged, contradictions resolved, concrete & correct

**Guiding choices**
- Use **schema-first + idempotent SQL seed** (fast, reliable, works in CI/CD and local) rather than runtime ORM bootstraps that can hide schema mismatches.
- Keep **setup script** minimal, safe, and dependency-light; do not assume `createdb`/`psql` are always available in all environments — detect and guide.
- Add **health/readiness checks** (code + endpoint) so LINE webhook and DB issues are visible early.
- Add **explicit LINE validation/smoke test** script so misconfigurations fail fast.
- Keep tenant bootstrap **idempotent and seed-driven**; provide a small service helper only for runtime needs (e.g., SaaS onboarding), but default to seed for first-run.
- Update README with clear, copy-paste quick-start and troubleshooting.

---

## 1) Files to add/modify

1. `workio/scripts/setup.sh` — dev environment + tenant bootstrap script (idempotent).
2. `workio/scripts/check-env.sh` — pre-flight checks for PostgreSQL, LINE tokens, and webhook reachability.
3. `workio/server/src/db/schema.sql` — (existing) ensure it is idempotent where possible (no destructive changes).
4. `workio/server/src/db/seed.sql` — minimal seed: roles, one default tenant, one SuperAdmin user (idempotent).
5. `workio/server/src/services/tenantService.ts` — optional runtime helper for tenant creation (kept simple).
6. `workio/server/src/server.ts` (or main app file) — add `/health` and `/ready` endpoints.
7. `workio/README.md` — add Quick start and troubleshooting.

---

## 2) Implementation

### 2.1 `workio/scripts/setup.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Workio setup =="

# 1) Check core deps
for cmd in node npm; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: $cmd not found. Install Node.js/npm."; exit 1; }
done

# 2) PostgreSQL checks (try to use DATABASE_URL or fall back)
DB_URL="${DATABASE_URL:-}"
if [[ -z "$DB_URL" ]]; then
  # Try to build from common env vars
  PGHOST="${PGHOST:-localhost}"
  PGPORT="${PGPORT:-5432}"
  PGDATABASE="${PGDATABASE:-workio}"
  PGUSER="${PGUSER:-postgres}"
  # If psql exists, try to create DB if missing
  if command -v psql >/dev/null 2>&1; then
    if ! psql -lqt -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" | cut -d \| -f 1 | grep -qw "$PGDATABASE"; then
      echo "Creating database '$PGDATABASE'..."
      createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE" || {
        echo "WARNING: could not create DB. Ensure PostgreSQL is running and credentials are correct."
      }
    else
      echo "Database '$PGDATABASE' exists."
    fi
    DB_URL="postgresql://${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}"
  else
    echo "WARNING: psql not found. Set DATABASE_URL manually and ensure the database exists."
  fi
fi

# 3) Copy .env if missing
SERVER_ENV="workio/server/.env"
if [[ ! -f "$SERVER_ENV" ]]; then
  if [[ -f "${SERVER_ENV}.example" ]]; then
    cp "${SERVER_ENV}.example" "$SERVER_ENV"
    echo "Created $SERVER_ENV from example."
  else
    echo "WARNING: .env.example not found. Create $SERVER_ENV manually."
  fi
fi

# 4) Run schema + seed (prefer psql if available; otherwise use node script)
if command -v psql >/dev/null 2>&1 && [[ -n "$DB_URL" ]]; then
  echo "Running schema..."
  psql "$DB_URL" < workio/server/src/db/schema.sql || {
    echo "WARNING: schema.sql failed. Check DB connection and schema compatibility."
  }

  if [[ -f "workio/server/src/db/seed.sql" ]]; then
    echo "Running seed..."
    psql "$DB_URL" < workio/server/src/db/seed.sql || {
      echo "WARNING: seed.sql failed. Check constraints/duplicates."
    }
  else
    echo "WARNING: seed.sql not found. Skipping seed."
  fi
else
  echo "INFO: psql not available or DB_URL missing. Run schema/seed manually or provide DATABASE_URL."
fi

# 5) Install deps (non-blocking)
echo "Installing server deps..."
(cd workio/server && npm install --silent) || echo "WARNING: server npm install failed."

echo "Installing frontend deps..."
(cd workio && npm install --silent) || echo "WARNING: frontend npm install failed."

echo ""
echo "== Setup complete =="
echo "Next steps:"
echo "1) Edit workio/server/.env and set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET"
echo "2) (Optional) Run ./scripts/check-env.sh to validate env and LINE connectivity"
echo "3) Start server: cd workio/server && npm run dev"
echo "4) Start frontend: cd workio && npm run dev"
echo ""
echo "Default tenant and admin are created by seed.sql (if applied)."
```

Make executable:
```bash
chmod +x workio/scripts/setup.sh
```

---

### 2.2 `workio/scripts/check-env.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Environment & LINE pre-flight checks =="

# Check .env
SERVER_ENV="workio/server/.env"
if [[ -f "$SERVER_ENV" ]]; then
  export $(grep -v '^#' "$SERVER_ENV" | xargs) || true
else
  echo "WARNING: $SERVER_ENV not found. Create it from .example."
fi

# Required vars
if [[ -z "${LINE_CHANNEL_ACCESS_TOKEN:-}" ]]; then
  echo "ERROR: LINE_CHANNEL_ACCESS_TOKEN not set in $SERVER_ENV"
  exit 1
fi
if [[ -z "${LINE_CHANNEL_SECRET:-}" ]]; then
  echo "ERROR: LINE_CHANNEL_SECRET not set in $SERVER_ENV"
  exit 1
fi

# Check PostgreSQL
DB_URL="${DATABASE_URL:-}"
if [[ -n "$DB_URL" ]] && command -v psql >/dev/null 2>&1; then
  if psql "$DB_URL" -c '\conninfo' >/dev/null 2>&1; then
    echo "OK: PostgreSQL reachable."
  else
    echo "ERROR: Cannot connect to PostgreSQL at $DB_URL"
    exit 1
  fi
else
  echo "WARNING: Cannot validate PostgreSQL (psql missing or DATABASE_URL not set)."
fi

# Validate LINE token (simple call to get profile endpoint via curl)
echo "Validating LINE Channel Access Token..."
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${LINE_CHANNEL_ACCESS_TOKEN}" \
  "https://api.line.me/oauth2/v2.1/verify")

if [[ "$HTTP_STATUS" == "200" ]]; then
  echo "OK: LINE Channel Access Token looks valid."
else
  echo "ERROR: LINE token validation failed (HTTP $HTTP_STATUS). Check token and scopes."
  exit 1
fi

# Check webhook reachability (basic DNS/HTTP check)
WEBHOOK_URL="${LINE_WEBHOOK_URL:-http://localhost:3000/webhook/line}"
echo "Checking webhook reachability (GET $WEBHOOK_URL)..."
if curl -s -f -o /dev/null --max-time 5 "$WEBHOOK_URL"; then
  echo "OK: Webhook endpoint responded."
else
  echo "WARNING: Webhook endpoint not reachable. For local dev, use cloudflared or ngrok and set LINE_WEBHOOK_URL."
fi

echo ""
echo "== Pre-flight checks complete =="
```

Make executable:
```bash
chmod +x workio/scripts/check-env.sh
```

---

### 2.3 `workio/server/src/db/seed.sql`
```sql
-- Idempotent seed for roles, default tenant, and SuperAdmin
