#!/usr/bin/env python3
"""axentx PRD daemon — terminal stage of product-discovery pipeline.

Pulls from prd-queue (full opportunity dossier: research → BD → design →
business → marketing). Decomposes into:

  1. PRD (1-2 page product requirements doc — markdown)
  2. Epic list (3-7 epics)
  3. User stories per epic (Connextra "As a <user>, I want <action>, so I
     can <outcome>" + acceptance criteria)
  4. Concrete tasks per story (small enough that the existing dev daemon
     can implement each in <2h)

Tasks are then individually pushed back to the existing dev-queue so the
SDLC pipeline (dev → review → qa → commit) auto-executes them. This is
the bridge from "we discovered an opportunity" to "code is being written
and committed to GitHub."

PRD itself + epics + stories are also dropped as a markdown decision
record so the existing commit daemon writes them into the appropriate
project repo (.axentx-dev-bot/prd-<id>.md).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop, new_item, write_item,
                             get_role_budget, get_portfolio_block,
                              get_portfolio,)

POLL_SEC = int(os.environ.get("PRD_POLL_SEC", "120"))
PRD_BUDGET = get_role_budget("prd", 3500)


PRD_SYSTEM = """You are a senior PM. For a validated opportunity, produce a
crisp PRD + epics + stories + tasks that engineers can implement TODAY.

Output STRICT JSON:

{
  "prd": {
    "title": "<one-line product/feature name>",
    "summary": "<3-sentence elevator pitch>",
    "problem": "<what user pain we solve, with evidence>",
    "audience": "<exact ICP>",
    "non_goals": ["<3 things we explicitly won't do in v1>"],
    "success_metric": "<the one number that defines done>",
    "kill_criteria": ["<3 testable conditions that, if met, mean we kill the product>"]
  },
  "epics": [
    {
      "id": "E1", "title": "<short>",
      "value": "<why this epic moves NSM>",
      "stories": [
        {
          "id": "E1-S1",
          "story": "As a <user>, I want <action>, so I can <outcome>.",
          "acceptance": ["<criterion 1>", "<criterion 2>", "<criterion 3>"],
          "tasks": [
            {"id": "E1-S1-T1", "title": "<small task <2h>", "files": ["<paths likely touched>"]}
          ]
        }
      ]
    }
  ]
}

Rules:
- 3-7 epics maximum (we're shipping a v1, not boiling the ocean).
- Each story has 3-5 acceptance criteria (testable).
- Each task has 1-3 likely file paths (under the target project repo).
- Tasks should be small — 'add cron field validation' yes, 'build entire
  auth subsystem' no. The dev daemon implements each task in <2h cycles.
- Specify the TARGET PROJECT path (e.g. /opt/axentx/Costinel) for files.
- kill_criteria are the 3 measurable failure conditions that, if observed
  post-launch, mean we shut the product down. Be specific (e.g. '<10 weekly
  active users after 6 weeks', 'CAC > LTV by 3x at month 3', 'NPS < 20')."""


def do_one_prd() -> bool:
    picked = pick_oldest("prd")
    if not picked: return False
    src_path, item = picked

    pain = item.get("verdict", {}) or {}
    bd = item.get("bd_verdict", {}) or {}
    design = item.get("design_verdict", {}) or {}
    biz = item.get("business_verdict", {}) or {}
    gtm = item.get("marketing_verdict", {}) or {}
    target = item.get("target_project") or bd.get("target_project") or "surrogate-1"

    log("prd", f"▸ {item['id'][:30]}  → {target}")
    prompt = (
        f"=== full opportunity dossier ===\n"
        f"Pain: {pain.get('pain_one_liner','?')}\n"
        f"BD verdict: {bd.get('verdict','?')} → target={target}\n"
        f"Hypothesis: {bd.get('feature_one_liner') or bd.get('new_product_one_liner','')}\n"
        f"Root cause: {design.get('root_cause','?')}\n"
        f"JTBD: {design.get('jtbd','?')}\n"
        f"BMC value props: {(biz.get('bmc') or {}).get('value_propositions','?')}\n"
        f"NSM: {biz.get('north_star_metric','?')}\n"
        f"Positioning: {gtm.get('positioning','?')}\n"
        f"ICP: {gtm.get('icp','?')}\n"
        f"Launch metric: {gtm.get('launch_metric','?')}\n\n"
        f"Target project repo: /opt/axentx/{target}\n\n"
        f"Output PRD + epics + stories + tasks (strict JSON, follow schema)."
    )
    try:
        out = call_llm(prompt,
                       system=PRD_SYSTEM + "\n\n" + get_portfolio_block(),
                       max_tokens=PRD_BUDGET, timeout=90)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        prd = json.loads(txt.strip())
    except Exception as e:
        fail(item, src_path, "prd", f"LLM/parse failed: {e}")
        log("prd", f"✗ {item['id']}: parse fail — {str(e)[:80]}")
        return True

    item["prd"] = prd
    item["history"].append({
        "stage": "prd",
        "actor": "axentx-prd",
        "output": json.dumps(prd, ensure_ascii=False),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    item["current"]["text"] = json.dumps(prd, ensure_ascii=False)

    # Push every task back into dev-queue so the existing engineering pipeline
    # picks them up. Each task becomes an independent dev item with full
    # provenance pointing back at this opportunity.
    n_tasks = 0
    for epic in (prd.get("epics") or []):
        for story in (epic.get("stories") or []):
            for task in (story.get("tasks") or []):
                ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                hsh = hashlib.sha256(
                    f"{item['id']}|{task.get('id','')}|{task.get('title','')}".encode()
                ).hexdigest()[:8]
                tid = f"{ts}-{target}-{epic.get('id','E?')}-{task.get('id','T?')}-{hsh}"
                dev_item = new_item(target, "feature", task.get("title", ""))
                dev_item["id"] = tid
                dev_item["from_prd"] = item["id"]
                dev_item["epic"] = epic.get("title", "")
                dev_item["story"] = story.get("story", "")
                dev_item["acceptance"] = story.get("acceptance", [])
                dev_item["files_hint"] = task.get("files", [])
                dev_item["history"] = [{
                    "stage": "dev",
                    "actor": "axentx-prd",
                    "output": (
                        f"Task derived from PRD {item['id']}.\n\n"
                        f"Story: {story.get('story','')}\n"
                        f"Acceptance:\n" +
                        "\n".join(f"  - {a}" for a in story.get('acceptance', [])) +
                        f"\n\nTask: {task.get('title','')}\n"
                        f"Likely files: {', '.join(task.get('files', []))}\n\n"
                        f"Implement and produce a concrete code diff."
                    ),
                    "at": datetime.datetime.utcnow().isoformat() + "Z",
                }]
                dev_item["current"]["text"] = dev_item["history"][0]["output"]
                write_item(dev_item, "dev")
                n_tasks += 1

    # Hand off to architect for RFC + epic breakdown into parallel tickets.
    # 2026-05-05: was "tech-lead" but that queue doesn't exist in QUEUES dict
    # (KeyError flooding logs). architect-queue exists and architect-daemon
    # already does the equivalent RFC+breakdown work.
    advance(item, src_path, "tech-lead", "prd",
            f"PRD-COMPLETE: {n_tasks} tasks staged, "
            f"{len(prd.get('epics', []))} epics → tech-lead for RFC + ticket breakdown")
    log("prd", f"  ✓ PRD → tech-lead (will break into RFC + tickets) "
               f"— {len(prd.get('epics', []))} epics, "
               f"{n_tasks} preliminary tasks already in dev-queue")
    return True


if __name__ == "__main__":
    daemon_loop("prd", POLL_SEC, do_one_prd)
