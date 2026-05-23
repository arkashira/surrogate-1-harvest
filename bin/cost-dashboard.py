#!/usr/bin/env python3
"""Aggregate usage across all free-tier services into a single dashboard.
Outputs JSON to state/cost-dashboard.json + markdown to docs/cost-dashboard.md.
Schedule via hermes-jobs.json hourly."""
from __future__ import annotations
import datetime, json, os, sys, urllib.request
from pathlib import Path
REPO_ROOT = Path(os.environ.get("REPO_ROOT","/opt/surrogate-1-harvest"))
OUT_JSON = REPO_ROOT / "state" / "cost-dashboard.json"
OUT_MD   = REPO_ROOT / "docs" / "cost-dashboard.md"

def cf_metrics():
    tok = os.environ.get("CLOUDFLARE_API_TOKEN")
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not (tok and acct): return {}
    out = {}
    try:
        req = urllib.request.Request(
            "https://surrogate-1-cursor.ashira.workers.dev/metrics",
            headers={"Accept":"text/plain"})
        with urllib.request.urlopen(req, timeout=10) as r:
            for line in r.read().decode().splitlines():
                if line.startswith("surrogate_cursor_requests"):
                    k = line.split('"')[1]; v = int(line.split()[-1])
                    out[k] = v
    except Exception as e: out["_err_metrics"] = str(e)
    return out

def supabase_metrics():
    pat = os.environ.get("SUPABASE_PAT")
    if not pat: return {}
    try:
        req = urllib.request.Request(
            "https://api.supabase.com/v1/projects",
            headers={"Authorization": f"Bearer {pat}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        return {"projects": len(d), "active": sum(1 for p in d if p.get("status")=="ACTIVE_HEALTHY")}
    except Exception as e: return {"_err": str(e)}

def hf_storage():
    """Approximate per-dataset size (we already know total from earlier audit)."""
    return {"axentx_pairs_total_tb": 9.56, "axentx_pairs_files": 24719,
            "snapshot_date": "2026-05-02"}

def gcp_load():
    """Run via subprocess on the box itself."""
    import subprocess
    try:
        m = subprocess.run(["free","-m"], capture_output=True, text=True, timeout=5)
        u = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        return {"free_h": m.stdout.splitlines()[1] if m.stdout else "",
                "uptime": u.stdout.strip()}
    except Exception as e: return {"_err": str(e)}

def main():
    data = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "cf_worker": cf_metrics(),
        "supabase":  supabase_metrics(),
        "hf_hub":    hf_storage(),
        "gcp":       gcp_load(),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, indent=2))

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    md = ["# Cost dashboard", f"_Generated: {data['ts']}_", "", "All services free-tier. Updated hourly.", ""]
    for section, kvs in data.items():
        if section == "ts": continue
        md.append(f"## {section}"); md.append("")
        if isinstance(kvs, dict):
            for k, v in kvs.items():
                md.append(f"- **{k}**: {v}")
        md.append("")
    OUT_MD.write_text("\n".join(md))
    print(json.dumps(data, indent=2))

if __name__ == "__main__":
    main()
