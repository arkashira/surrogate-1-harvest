#!/usr/bin/env python3
"""axentx architect — drafts system architecture for NEW-PRODUCT items.

Picks from prd-queue any item where bd_verdict.verdict == 'NEW-PRODUCT'.
Outputs an Architecture Decision Record (ADR) in markdown — folder
structure, tech stack pick (with rationale), key data model, third-party
deps, deployment topology. Pushes the ADR back into prd-queue (so the PRD
daemon then knows scaffold paths and can produce more accurate task
hints), and writes the ADR markdown into swarm-shared/decisions/.
"""
from __future__ import annotations
import datetime, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop, rag_query, get_portfolio_block,
                              get_portfolio,)
POLL_SEC = int(os.environ.get("ARCH_POLL_SEC", "120"))

ARCH_SYSTEM = """You are a principal engineer drafting an Architecture
Decision Record (ADR) for a brand-new product. Output strict JSON:

{
  "tech_stack": {"backend":"<lang+framework with rationale>", "frontend":"<or 'none'>", "datastore":"<sql|nosql|both — name>", "queue":"<if needed>", "cache":"<if needed>"},
  "folder_layout": ["<src/...>", "<tests/...>", "..."],
  "data_model": [{"entity":"<name>","fields":[{"name":"...","type":"...","note":"..."}]}],
  "deployment": "<gcp e2-micro|cf workers|hf space|kaggle|hybrid>",
  "third_party": ["<critical lib + reason>"],
  "open_questions": ["<unknowns the team must answer in week 1>"],
  "first_release_scope": "<what v0.1 ships in 2 weeks>"
}

Rules: free-tier first (cloud-free hosts, OSS deps). Reuse our existing
infra (Supabase, CF, HF) where it fits. Be specific — 'Postgres' not 'a
database'. Output JSON only, no prose."""

def do_one() -> bool:
    picked = pick_oldest("prd")  # we sample prd-queue items routed for NEW-PRODUCT
    if not picked: return False
    src_path, item = picked
    bd = item.get("bd_verdict", {}) or {}
    if (bd.get("verdict") or "").upper() != "NEW-PRODUCT":
        # not for us — push back unchanged (FIFO will eventually let prd see it)
        return False
    pain = item.get("verdict", {}) or {}
    design = item.get("design_verdict", {}) or {}
    biz = item.get("business_verdict", {}) or {}
    log("architect", f"▸ {item['id'][:32]}  product hypothesis: {bd.get('new_product_one_liner','')[:60]}")
    rag = rag_query(
        f"system architecture for: {bd.get('new_product_one_liner','')} "
        f"audience: {pain.get('audience','')}", top_k=5
    ) or ""
    prompt = (
        f"=== product brief ===\n"
        f"Hypothesis: {bd.get('new_product_one_liner','?')}\n"
        f"JTBD: {design.get('jtbd','?')}\n"
        f"Audience: {pain.get('audience','?')}\n"
        f"BMC value props: {(biz.get('bmc') or {}).get('value_propositions','?')}\n"
        f"NSM: {biz.get('north_star_metric','?')}\n\n"
        f"{rag}\n\nDraft ADR. Strict JSON only."
    )
    try:
        out = call_llm(prompt,
                       system=ARCH_SYSTEM + "\n\n" + get_portfolio_block(),
                       max_tokens=2000, timeout=60)
        txt = out.strip()
        if "```" in txt: txt = txt.split("```")[1]
        if txt.startswith("json"): txt = txt[4:]
        adr = json.loads(txt.strip())
    except Exception as e:
        fail(item, src_path, "architect", f"LLM/parse fail: {e}")
        return True
    item["architecture"] = adr
    item["history"].append({"stage":"architect","actor":"axentx-architect",
                            "output":json.dumps(adr,ensure_ascii=False),
                            "at":datetime.datetime.utcnow().isoformat()+"Z"})
    # write ADR markdown into decisions for repo + RAG indexing
    ddir = REPO_ROOT/"state"/"swarm-shared"/"decisions"
    ddir.mkdir(parents=True, exist_ok=True)
    md = (
        f"# ADR — {bd.get('new_product_one_liner','?')[:80]}\n\n"
        f"- id: `{item['id']}`\n- target: NEW-PRODUCT\n\n"
        f"## Stack\n```json\n{json.dumps(adr.get('tech_stack',{}), indent=2)}\n```\n\n"
        f"## Folder layout\n" + "\n".join(f"- `{p}`" for p in adr.get('folder_layout',[])) + "\n\n"
        f"## Data model\n```json\n{json.dumps(adr.get('data_model',[]), indent=2)}\n```\n\n"
        f"## Deployment\n{adr.get('deployment','?')}\n\n"
        f"## v0.1 scope\n{adr.get('first_release_scope','?')}\n\n"
        f"## Open questions\n" + "\n".join(f"- {q}" for q in adr.get('open_questions',[]))
    )
    (ddir / f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}_adr_{item['id'][:24]}.md").write_text(md)
    advance(item, src_path, "prd", "architect", json.dumps(adr,ensure_ascii=False))
    log("architect", f"  ✓ ADR written, item back to prd-queue with arch context")
    return True

if __name__ == "__main__":
    daemon_loop("architect", POLL_SEC, do_one)
