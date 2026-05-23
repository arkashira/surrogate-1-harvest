#!/usr/bin/env bash
# Deploy axentx-waitlist Cloudflare Worker + KV namespace.
# Idempotent — safe to re-run. Uses CLOUDFLARE_API_TOKEN from env file.

set -euo pipefail

CT=$(grep "^CLOUDFLARE_API_TOKEN=" /etc/surrogate-coordinator.env | cut -d= -f2-)
ACCT="77fb5e6c3716be794dc3e8467ba9f285"
WORKER="axentx-waitlist"
KV_TITLE="axentx-waitlist"

if [ -z "$CT" ]; then
  echo "✗ CLOUDFLARE_API_TOKEN not set"; exit 1
fi

echo "[1/5] ensure KV namespace '$KV_TITLE'"
KV_ID=$(curl -s -H "Authorization: Bearer $CT" \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/storage/kv/namespaces?per_page=50" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
title='$KV_TITLE'
for ns in d.get('result',[]):
    if ns['title']==title:
        print(ns['id']); break
")

if [ -z "$KV_ID" ]; then
  KV_ID=$(curl -s -X POST -H "Authorization: Bearer $CT" -H "Content-Type: application/json" \
    "https://api.cloudflare.com/client/v4/accounts/$ACCT/storage/kv/namespaces" \
    -d "{\"title\":\"$KV_TITLE\"}" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d['result']['id'])
")
  echo "  created KV: $KV_ID"
else
  echo "  found KV:   $KV_ID"
fi

echo "[2/5] write worker.js"
WJS="/tmp/axentx-waitlist-worker.js"
cat > "$WJS" <<'JS_EOF'
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};
const J = (v) => ({ "content-type": "application/json", ...CORS, ...v });

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    // POST /waitlist/<slug>
    const wm = url.pathname.match(/^\/waitlist\/([a-zA-Z0-9_-]{1,64})$/);
    if (wm && req.method === "POST") {
      const slug = wm[1];
      let email = "";
      try {
        const ct = req.headers.get("content-type") || "";
        if (ct.includes("application/json")) {
          const body = await req.json();
          email = body.email || "";
        } else {
          const fd = await req.formData();
          email = fd.get("email") || "";
        }
      } catch (e) {}

      email = String(email).trim().toLowerCase();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 200) {
        return new Response(JSON.stringify({ ok: false, error: "invalid_email" }),
          { status: 400, headers: J() });
      }

      const ts = new Date().toISOString();
      const ip = req.headers.get("cf-connecting-ip") || "";
      const ua = (req.headers.get("user-agent") || "").slice(0, 250);
      const country = (req.cf && req.cf.country) || "";
      const referer = (req.headers.get("referer") || "").slice(0, 250);
      const key = `entry:${slug}:${email}`;

      const existing = await env.WAITLIST.get(key);
      if (!existing) {
        const rec = { email, ts, ip, ua, country, referer, slug };
        await env.WAITLIST.put(key, JSON.stringify(rec),
          { expirationTtl: 60 * 60 * 24 * 365 });
        const cntKey = `count:${slug}`;
        const curr = parseInt((await env.WAITLIST.get(cntKey)) || "0");
        await env.WAITLIST.put(cntKey, String(curr + 1));
      }

      return new Response(JSON.stringify({ ok: true, slug }),
        { status: 200, headers: J() });
    }

    // GET /count/<slug>
    const cm = url.pathname.match(/^\/count\/([a-zA-Z0-9_-]{1,64})$/);
    if (cm && req.method === "GET") {
      const slug = cm[1];
      const count = parseInt((await env.WAITLIST.get(`count:${slug}`)) || "0");
      return new Response(JSON.stringify({ slug, count }),
        { status: 200, headers: J() });
    }

    // GET /list/<slug>?secret=<TOKEN> — admin export
    const lm = url.pathname.match(/^\/list\/([a-zA-Z0-9_-]{1,64})$/);
    if (lm && req.method === "GET") {
      const secret = url.searchParams.get("secret") || "";
      if (secret !== env.ADMIN_SECRET) {
        return new Response(JSON.stringify({ ok: false, error: "unauthorized" }),
          { status: 401, headers: J() });
      }
      const slug = lm[1];
      const list = await env.WAITLIST.list({ prefix: `entry:${slug}:`, limit: 1000 });
      const rows = [];
      for (const k of list.keys) {
        const v = await env.WAITLIST.get(k.name);
        if (v) try { rows.push(JSON.parse(v)); } catch {}
      }
      return new Response(JSON.stringify({ slug, count: rows.length, rows }, null, 2),
        { status: 200, headers: J() });
    }

    if (url.pathname === "/" || url.pathname === "") {
      return new Response(
        "axentx-waitlist · POST /waitlist/<slug> {email} · GET /count/<slug> · GET /list/<slug>?secret=…",
        { status: 200, headers: { "content-type": "text/plain", ...CORS } });
    }

    return new Response("not found", { status: 404, headers: CORS });
  },
};
JS_EOF

echo "[3/5] generate ADMIN_SECRET"
ADMIN_SECRET_FILE="/etc/axentx-waitlist-admin.secret"
if [ ! -f "$ADMIN_SECRET_FILE" ]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$ADMIN_SECRET_FILE"
  chmod 600 "$ADMIN_SECRET_FILE"
fi
ADMIN_SECRET=$(cat "$ADMIN_SECRET_FILE")

echo "[4/5] deploy worker '$WORKER' with KV binding"
META=$(python3 -c "
import json
print(json.dumps({
  'main_module': 'worker.js',
  'compatibility_date': '2025-01-01',
  'compatibility_flags': ['nodejs_compat'],
  'bindings': [
    {'name': 'WAITLIST', 'type': 'kv_namespace', 'namespace_id': '$KV_ID'},
    {'name': 'ADMIN_SECRET', 'type': 'secret_text', 'text': '$ADMIN_SECRET'},
  ],
}))")

# Multipart: metadata + worker.js
BOUND="----axentxBoundary$(date +%s)"
TMPBODY=$(mktemp)
{
  printf -- "--%s\r\n" "$BOUND"
  printf 'Content-Disposition: form-data; name="metadata"\r\n'
  printf 'Content-Type: application/json\r\n\r\n'
  printf "%s\r\n" "$META"
  printf -- "--%s\r\n" "$BOUND"
  printf 'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
  printf 'Content-Type: application/javascript+module\r\n\r\n'
  cat "$WJS"
  printf "\r\n--%s--\r\n" "$BOUND"
} > "$TMPBODY"

RESP=$(curl -s -X PUT \
  -H "Authorization: Bearer $CT" \
  -H "Content-Type: multipart/form-data; boundary=$BOUND" \
  --data-binary "@$TMPBODY" \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/workers/scripts/$WORKER")
rm -f "$TMPBODY"

echo "$RESP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('success'):
    print('  ✓ deployed')
else:
    print('  ✗ deploy failed:', json.dumps(d.get('errors',[])[:3], indent=2))
    sys.exit(1)
"

echo "[5/5] enable workers.dev subdomain"
curl -s -X POST -H "Authorization: Bearer $CT" -H "Content-Type: application/json" \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/workers/scripts/$WORKER/subdomain" \
  -d '{"enabled":true}' | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  subdomain enabled:', d.get('result',{}).get('enabled', False))
"

# Capture worker.dev subdomain
SUB=$(curl -s -H "Authorization: Bearer $CT" \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/workers/subdomain" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['subdomain'])")

URL="https://${WORKER}.${SUB}.workers.dev"
echo
echo "✓ Worker live at: $URL"
echo "  Endpoints:"
echo "    POST $URL/waitlist/<slug>   {\"email\":\"...\"}"
echo "    GET  $URL/count/<slug>"
echo "    GET  $URL/list/<slug>?secret=<ADMIN_SECRET>"
echo
echo "ADMIN_SECRET (use for /list export):"
echo "  $ADMIN_SECRET"

# Save URL for landing-gen to consume
mkdir -p /opt/surrogate-1-harvest/state
echo "$URL" > /opt/surrogate-1-harvest/state/waitlist-endpoint.url
echo "  saved to /opt/surrogate-1-harvest/state/waitlist-endpoint.url"
