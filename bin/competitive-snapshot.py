#!/usr/bin/env python3
"""Snapshot competitive landscape per axentx product. Runs weekly via cron."""
from __future__ import annotations
import datetime, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import REPO_ROOT, log, call_llm

PROJECTS = {
    "Costinel":  "AWS cost analytics + anomaly detection",
    "vanguard":  "Cloud security posture management (CSPM)",
    "airship":   "IaC / multi-cloud deployment + DevSecOps integrated tooling",
    "workio":    "Workflow automation (engineering teams)",
    "surrogate-1": "Autonomous AI dev agent",
    # axiomops dropped 2026-05-02 — merged into airship (target shifted)
}
OUT_DIR = REPO_ROOT / "docs" / "competitive"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SYS = """For the given product, output strict JSON:
{
 "competitors": [{"name":"...","website":"...","positioning":"...","weakness":"<our exploit>"}],
 "white_space": "<what's underserved>",
 "moat_for_axentx": "<our specific advantage in 1 sentence>"
}
List 5-8 competitors. Cite real company names only — if you don't know a competitor in this space, say so honestly."""

def main():
    for slug, desc in PROJECTS.items():
        try:
            out = call_llm(
                f"Product: {slug}\nWhat it does: {desc}\n\nMap competitive landscape.",
                system=SYS, max_tokens=1500, timeout=60)
            txt = out.strip()
            if "```" in txt: txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
            d = json.loads(txt.strip())
        except Exception as e:
            log("comp-snap", f"  ✗ {slug}: {e}"); continue
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        path = OUT_DIR / f"{slug}-{ts}.md"
        md = [f"# Competitive snapshot — {slug}", f"_{ts}_", "",
              f"**Product**: {desc}", "", "## Competitors", ""]
        for c in d.get("competitors", []):
            md.append(f"### {c.get('name','?')}")
            md.append(f"- Website: {c.get('website','?')}")
            md.append(f"- Positioning: {c.get('positioning','?')}")
            md.append(f"- Weakness we exploit: {c.get('weakness','?')}")
            md.append("")
        md.append(f"## White space\n{d.get('white_space','?')}\n")
        md.append(f"## Our moat\n{d.get('moat_for_axentx','?')}\n")
        path.write_text("\n".join(md))
        log("comp-snap", f"  ✓ {slug} → {path.name}")

if __name__ == "__main__":
    main()
