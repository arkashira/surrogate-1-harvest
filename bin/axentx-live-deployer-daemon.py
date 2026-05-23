#!/usr/bin/env python3
"""axentx Live-Deployer — push generated landing pages to Cloudflare Pages.

User direction 2026-05-11:
  > 'product ที่เป็นที่ต้องการ มากที่สุด live ได้เร็วที่สุด'

Reads /opt/surrogate-1-harvest/state/top-products.json + the static landings
under /opt/axentx-live/{slug}/, and pushes each one to its own Cloudflare
Pages project so each is served at:

    https://{slug}.pages.dev/

Why Cloudflare Pages (not GitHub Pages)?
  • arkashira repos are PRIVATE — free GitHub Pages requires public repos
  • CF Pages: free, unlimited projects, instant <slug>.pages.dev URL
  • Already have CF account + token in /etc/surrogate-coordinator.env

Direct Upload API flow per product:
  1. ensure project exists (POST /pages/projects, idempotent on 409)
  2. hash each file with BLAKE3 (first 32 hex chars of content+ext)
  3. POST /pages/projects/{slug}/upload-token  → JWT
  4. POST /pages/assets/check-missing          → which hashes need upload
  5. POST /pages/assets/upload                 → upload missing files
  6. POST /pages/projects/{slug}/deployments   → manifest → deploy
  7. record live URL in /opt/surrogate-1-harvest/state/live-urls.json
  8. update D1 portfolio: append `· LIVE: <url>` to product description

Cycle: 30 min. Idempotent — re-running creates new deployment if content
changed (CF auto-dedup matches identical hashes).
"""
from __future__ import annotations
import base64
import datetime
import json
import mimetypes
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

try:
    import blake3 as _blake3  # type: ignore
except ImportError:  # pragma: no cover
    _blake3 = None

CYCLE_SEC = int(os.environ.get("LIVE_DEPLOYER_CYCLE_SEC", "1800"))
TOP_PRODUCTS_FILE = REPO_ROOT / "state" / "top-products.json"
LIVE_URLS_FILE = REPO_ROOT / "state" / "live-urls.json"
LIVE_DIR = Path("/opt/axentx-live")
MIN_SCORE = int(os.environ.get("LIVE_MIN_SCORE", "30"))
MAX_PER_CYCLE = int(os.environ.get("LIVE_MAX_PER_CYCLE", "10"))
ACCT = "77fb5e6c3716be794dc3e8467ba9f285"

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("live-deployer", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _cf_token():
    r = subprocess.run(
        ["bash", "-c",
         "grep '^CLOUDFLARE_API_TOKEN=' /etc/surrogate-coordinator.env"
         " | cut -d= -f2-"],
        capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def _http(method, url, headers=None, data=None, timeout=60):
    """Tiny HTTP wrapper. Returns (status, body_text)."""
    h = {"User-Agent": "axentx-live-deployer/1.0"}
    if headers:
        h.update(headers)
    body = data
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, str(e)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _normalize_slug(s):
    """CF Pages project name: lowercase alphanumeric + hyphen, ≤58 chars."""
    import re
    out = re.sub(r"[^a-z0-9-]", "-", s.lower()).strip("-")
    out = re.sub(r"-+", "-", out)
    return out[:58]


def _ensure_project(token, project_name):
    """Create CF Pages project (idempotent). Returns True on success/exists."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/pages/projects"
    status, body = _http(
        "POST", url,
        headers={"Authorization": f"Bearer {token}"},
        data={"name": project_name, "production_branch": "main"})
    if status == 200:
        return True
    # Already exists → 409 or 400 with specific message
    if "already exists" in body.lower() or status == 409:
        return True
    if status == 400 and "duplicate" in body.lower():
        return True
    log("live-deployer",
        f"  ⚠ create project {project_name} {status}: {body[:200]}")
    return False


def _get_upload_jwt(token, project_name):
    url = (f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
           f"/pages/projects/{project_name}/upload-token")
    status, body = _http("GET", url,
                          headers={"Authorization": f"Bearer {token}"})
    try:
        d = json.loads(body)
        if d.get("success"):
            return d["result"]["jwt"]
    except Exception:
        pass
    log("live-deployer",
        f"  ⚠ get JWT {project_name} {status}: {body[:200]}")
    return None


def _hash_file(content, ext):
    """BLAKE3(content + ext) first 32 hex chars (CF Pages convention)."""
    h = _blake3.blake3(content + ext.encode()).hexdigest()
    return h[:32]


def _check_missing(jwt, hashes):
    """POST /pages/assets/check-missing → list of hashes that need upload."""
    url = "https://api.cloudflare.com/client/v4/pages/assets/check-missing"
    # Per CF docs: body is a JWT-signed payload. The official wrangler sends:
    # {"hashes": [...]} as JSON.  Bearer JWT.
    status, body = _http(
        "POST", url,
        headers={"Authorization": f"Bearer {jwt}"},
        data={"hashes": hashes})
    try:
        d = json.loads(body)
        if d.get("success"):
            return d.get("result") or []
    except Exception:
        pass
    log("live-deployer", f"  ⚠ check-missing {status}: {body[:200]}")
    return None


def _upload_assets(jwt, payload):
    """POST /pages/assets/upload — array of {key, value, metadata, base64}."""
    url = "https://api.cloudflare.com/client/v4/pages/assets/upload"
    status, body = _http(
        "POST", url,
        headers={"Authorization": f"Bearer {jwt}"},
        data=payload, timeout=120)
    try:
        d = json.loads(body)
        if d.get("success"):
            return True
    except Exception:
        pass
    log("live-deployer", f"  ⚠ upload {status}: {body[:200]}")
    return False


def _create_deployment(token, project_name, manifest):
    """POST /pages/projects/{name}/deployments  — multipart with manifest."""
    url = (f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
           f"/pages/projects/{project_name}/deployments")
    boundary = "----axentxLDP" + str(int(time.time()))
    body_parts = []
    body_parts.append(f"--{boundary}\r\n".encode())
    body_parts.append(
        b'Content-Disposition: form-data; name="manifest"\r\n\r\n')
    body_parts.append(json.dumps(manifest).encode())
    body_parts.append(f"\r\n--{boundary}--\r\n".encode())
    payload = b"".join(body_parts)
    status, resp = _http(
        "POST", url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=payload, timeout=120)
    try:
        d = json.loads(resp)
        if d.get("success"):
            return d["result"].get("url") or d["result"].get(
                "aliases", [None])[0]
    except Exception:
        pass
    log("live-deployer",
        f"  ⚠ create-deployment {project_name} {status}: {resp[:300]}")
    return None


def deploy_one(token, original_slug):
    """Deploy a single product's static dir. Returns live URL or None."""
    project_name = _normalize_slug(original_slug)
    src = LIVE_DIR / original_slug
    if not src.is_dir():
        log("live-deployer", f"  ⊘ {original_slug}: no landing dir")
        return None
    if _blake3 is None:
        log("live-deployer",
            "  ✗ blake3 not installed — pip install blake3")
        return None

    # 1. Ensure project
    if not _ensure_project(token, project_name):
        return None

    # 2. Hash files
    manifest = {}        # path -> hash
    files_by_hash = {}   # hash -> {content, ext, content_type}
    for fp in src.rglob("*"):
        if not fp.is_file():
            continue
        try:
            content = fp.read_bytes()
        except Exception:
            continue
        rel = "/" + str(fp.relative_to(src))
        ext = (fp.suffix.lstrip(".") or "bin").lower()
        h = _hash_file(content, ext)
        manifest[rel] = h
        ct = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
        files_by_hash[h] = {
            "content": content, "ext": ext, "content_type": ct,
        }
    if not manifest:
        log("live-deployer", f"  ⊘ {original_slug}: no files to deploy")
        return None

    # 3. Get JWT
    jwt = _get_upload_jwt(token, project_name)
    if not jwt:
        return None

    # 4. Check missing
    missing = _check_missing(jwt, list(files_by_hash.keys()))
    if missing is None:
        return None

    # 5. Upload missing
    if missing:
        payload = []
        for h in missing:
            f = files_by_hash.get(h)
            if not f:
                continue
            payload.append({
                "key": h,
                "value": base64.b64encode(f["content"]).decode(),
                "metadata": {"contentType": f["content_type"]},
                "base64": True,
            })
        if payload and not _upload_assets(jwt, payload):
            return None

    # 6. Create deployment
    url = _create_deployment(token, project_name, manifest)
    if url:
        # Strip protocol — CF returns full https://...pages.dev or alias
        if not url.startswith("http"):
            url = f"https://{url}"
        log("live-deployer",
            f"  ✓ LIVE {original_slug} → {url} ({len(manifest)} files)")
    return url


def _update_d1_live_url(slug, url, token):
    """Append `· LIVE: <url>` to D1 portfolio entry. Best-effort."""
    try:
        url_d1 = ("https://api.cloudflare.com/client/v4/accounts/"
                  f"{ACCT}/d1/database/"
                  "ae95ac58-7b7e-40d9-8708-518c23281ae6/query")
        # Read
        status, body = _http(
            "POST", url_d1,
            headers={"Authorization": f"Bearer {token}"},
            data={"sql": "SELECT v FROM kv_store WHERE k=?",
                  "params": ["bd.portfolio"]})
        if status != 200:
            return False
        d = json.loads(body)
        portfolio = json.loads(d["result"][0]["results"][0]["v"])
        products = portfolio.get("products") or {}
        desc = products.get(slug) or ""
        if url in desc:
            return True
        import re
        desc = re.sub(r"\s*·\s*LIVE:[^·]*", "", desc).strip()
        new_desc = f"{desc} · LIVE: {url}".strip()
        products[slug] = new_desc
        portfolio["products"] = products
        _http("POST", url_d1,
              headers={"Authorization": f"Bearer {token}"},
              data={"sql":
                    "INSERT OR REPLACE INTO kv_store (k,v,ts) VALUES (?,?,?)",
                    "params": ["bd.portfolio",
                               json.dumps(portfolio, ensure_ascii=False),
                               int(time.time())]})
        return True
    except Exception as e:
        log("live-deployer",
            f"  ⚠ D1 update {slug}: {type(e).__name__}: {str(e)[:120]}")
        return False


def main():
    log("live-deployer",
        f"start — cycle={CYCLE_SEC}s, min_score={MIN_SCORE}, "
        f"max/cycle={MAX_PER_CYCLE} → <slug>.pages.dev")
    if _blake3 is None:
        log("live-deployer",
            "✗ blake3 missing. install: pip install blake3 --break-system-packages")
        return 1
    while not _stop:
        try:
            token = _cf_token()
            if not token:
                log("live-deployer", "✗ CF token missing in env file")
                time.sleep(60)
                continue
            if not TOP_PRODUCTS_FILE.is_file():
                log("live-deployer",
                    f"⊘ no {TOP_PRODUCTS_FILE} yet — waiting")
            else:
                top = json.loads(TOP_PRODUCTS_FILE.read_text())
                ranked = top.get("all_ranked") or []
                eligible = [p for p in ranked
                            if p.get("score", 0) >= MIN_SCORE][:MAX_PER_CYCLE]
                log("live-deployer",
                    f"▸ {len(eligible)} products eligible "
                    f"(score≥{MIN_SCORE})")

                live_urls = {}
                if LIVE_URLS_FILE.is_file():
                    try:
                        live_urls = json.loads(LIVE_URLS_FILE.read_text())
                    except Exception:
                        live_urls = {}

                deployed_now = 0
                for p in eligible:
                    if _stop:
                        break
                    slug = p["slug"]
                    url = deploy_one(token, slug)
                    if url:
                        deployed_now += 1
                        live_urls[slug] = {
                            "url": url,
                            "score": p["score"],
                            "deployed_at": (datetime.datetime
                                            .utcnow().isoformat() + "Z"),
                            "project_name": _normalize_slug(slug),
                        }
                        _update_d1_live_url(slug, url, token)
                    # gentle pacing — CF Pages rate limits
                    time.sleep(2)

                LIVE_URLS_FILE.parent.mkdir(parents=True, exist_ok=True)
                LIVE_URLS_FILE.write_text(json.dumps(
                    live_urls, indent=2, ensure_ascii=False))
                log("live-deployer",
                    f"✓ deploy cycle done — deployed_now={deployed_now}, "
                    f"total_live={len(live_urls)}")
        except Exception as e:
            log("live-deployer",
                f"⚠ cycle crashed: {type(e).__name__}: {str(e)[:160]}")
        for _ in range(CYCLE_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
