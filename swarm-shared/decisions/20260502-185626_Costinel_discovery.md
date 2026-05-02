# Costinel / discovery

## Final Synthesis — One Concrete, Correct Plan

**Core problem to solve:**  
Costinel lacks machine-readable discovery of *what it can ingest, what signals it produces, and whether the environment is ready to run*. This increases time-to-value and causes misconfiguration.  
We must add a lightweight, deterministic discovery surface (CLI + optional route) that is **correct, actionable, and immediately usable** — without requiring database changes or external calls beyond simple env checks.

---

## 1. What to build (scope + boundaries)

- **Single new module**: `src/discovery.py`
  - CLI: `python -m costinel discovery` (and optional `--json`, `--check`)
  - Optional HTTP route: `GET /discovery` returning JSON (if web layer exists)
- **No database changes, no external API calls** (only `os.getenv` and local checks)
- **No automated ingestion of external docs** (avoid speculative scope; keep deterministic and maintainable)
- **Update README.md** with a Discovery section and quick command

**Why this scope:**  
- Solves the “no entrypoint” and “no connector inventory” problems concretely.  
- Avoids speculative “auto-seed from docs” that would introduce complexity, correctness risk, and maintenance burden.  
- Keeps time-to-value high and risk low.

---

## 2. Canonical capability manifest (correct + actionable)

Embed these as constants in `src/discovery.py`. They are the source of truth for what Costinel supports.

```python
SUPPORTED_CLOUDS = [
    {
        "name": "AWS",
        "provider": "aws",
        "features": ["cost_explorer", "reservation_utilization"],
        "required_permissions": [
            "ce:GetCostAndUsage",
            "ce:GetReservationUtilization",
            "ce:GetReservationCoverage",
            "organizations:DescribeOrganization",
            "iam:ListAccountAliases",
        ],
        "recommended_policies": [
            "arn:aws:iam::aws:policy/ReadOnlyAccess",
        ],
        "env_vars": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"],
        "optional_env": ["AWS_SESSION_TOKEN"],
        "sample_config": {
            "provider": "aws",
            "payer_account_id": "123456789012",
            "linked_accounts": ["111111111111", "222222222222"],
            "regions": ["us-east-1", "eu-west-1"],
            "granularity": "DAILY",
            "lookback_days": 30,
        },
    },
    {
        "name": "Google Cloud",
        "provider": "gcp",
        "features": ["billing_reports"],
        "required_permissions": [
            "billing.accounts.get",
            "billing.resourceCosts.list",
            "cloudresourcemanager.projects.list",
        ],
        "recommended_policies": [
            "roles/billing.viewer",
            "roles/resourcemanager.projectViewer",
        ],
        "env_vars": ["GOOGLE_APPLICATION_CREDENTIALS"],
        "optional_env": [],
        "sample_config": {
            "provider": "gcp",
            "billing_account_id": "012345-6789AB-CDEF01",
            "projects": ["my-project"],
            "export_dataset": "costinel_billing",
            "lookback_days": 30,
        },
    },
    {
        "name": "Azure",
        "provider": "azure",
        "features": ["cost_management"],
        "required_permissions": [
            "Microsoft.Consumption/usageDetails/read",
            "Microsoft.Billing/billingAccounts/read",
            "Microsoft.Resources/subscriptions/read",
        ],
        "recommended_policies": [
            "Reader role on subscription or management group",
        ],
        "env_vars": ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET"],
        "optional_env": ["AZURE_SUBSCRIPTION_ID"],
        "sample_config": {
            "provider": "azure",
            "tenant_id": "t-tenant",
            "subscription_id": "sub-123",
            "granularity": "DAILY",
            "lookback_days": 30,
        },
    },
]

SIGNAL_CATALOG = [
    {
        "type": "anomaly",
        "category": "spend",
        "name": "daily_spend_spike",
        "description": "Detects day-over-day spend increases beyond threshold.",
        "fields": ["account_id", "service", "delta_pct", "threshold_pct", "window"],
    },
    {
        "type": "anomaly",
        "category": "efficiency",
        "name": "low_utilization_instance",
        "description": "Identifies running instances with low CPU/network utilization.",
        "fields": ["resource_id", "avg_cpu", "avg_network", "running_hours"],
    },
    {
        "type": "recommendation",
        "category": "ri",
        "name": "ri_coverage",
        "description": "Suggests Reserved Instance purchases or modifications based on usage.",
        "fields": ["resource_type", "current_coverage_pct", "recommended_commitment"],
    },
    {
        "type": "recommendation",
        "category": "scheduling",
        "name": "stop_schedule",
        "description": "Proposes start/stop schedules for non-production resources.",
        "fields": ["resource_id", "proposed_schedule", "estimated_savings"],
    },
    {
        "type": "governance",
        "category": "policy",
        "name": "budget_alert",
        "description": "Budget threshold alerts with escalation suggestions.",
        "fields": ["budget_id", "threshold_pct", "current_spend", "action"],
    },
]
```

---

## 3. Readiness checks (correct + concrete)

Implement deterministic local checks only:

```python
def check_env(vars_to_check):
    return {v: {"set": bool(os.getenv(v))} for v in vars_to_check}

def readiness_report():
    report = {"clouds": {}, "overall_ready": True}
    for c in SUPPORTED_CLOUDS:
        env_report = check_env(c["env_vars"])
        missing = [k for k, v in env_report.items() if not v["set"]]
        ready = not missing
        if not ready:
            report["overall_ready"] = False
        report["clouds"][c["provider"]] = {
            "name": c["name"],
            "ready": ready,
            "missing_env": missing,
        }
    return report
```

**No network calls** — only presence checks. If users want deeper validation (e.g., credential reachability), they should run a targeted connector test command later.

---

## 4. CLI + optional route (actionable)

`src/discovery.py` main behavior:

```python
def build_payload():
    return {
        "product": "Costinel",
        "version": "4.2.0",
        "capabilities": {
            "supported_clouds": SUPPORTED_CLOUDS,
            "signal_catalog": SIGNAL_CATALOG,
        },
        "readiness": readiness_report(),
        "next_steps": [
            "Set required environment variables for your cloud provider(s).",
            "Run: python -m costinel discovery --check to validate readiness.",
            "Configure a cloud connector using a sample config.",
            "Ingest cost data and review signals in the dashboard.",
        ],
    }

def main():
    import argparse, json, sys
    parser = argparse.ArgumentParser(description="Costinel discovery")
    parser.add_argument("--check", action="store_true", help="Run readiness checks")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    payload = build_payload()

    if args.json:
        print(json.dumps(payload, indent=2))
        sys.exit(0 if payload["readiness"]["overall_ready"] else 2)

    print("Costinel — Discovery")
    print("=" * 48)
    print(f"Version: {payload['version']}\n")

    print("Supported Clouds:")
    for c in payload["capabilities"]["supported_clouds"]:
        r = payload["readiness"]["clouds"][c["provider"]]

