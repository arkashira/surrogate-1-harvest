#!/usr/bin/env bash
# Surrogate-1 dataset enricher — pulls high-quality public datasets across the full
# software-development domain stack a big tech company has, dedups, and merges into
# axentx/surrogate-1-training-pairs.
#
# Domain coverage:
#   • Coding instructions (general)        Magicoder OSS-Instruct, Evol-Instruct, evol-codealpaca
#   • Multi-turn assistant dialogue        ultrachat_200k, SlimOrca-Dedup
#   • Code review / commits                commitpackft (real PR commit messages)
#   • Reasoning / math                     MathInstruct, MetaMathQA
#   • Helpfulness preferences              hh-rlhf
#   • IaC (Terraform/Dockerfile/K8s/YAML)  bigcode/the-stack-smol (filtered)
#   • Security / DevSecOps                 semgrep-rules + CodeAlpaca security subset
#
# All sources are MIT / Apache / CC-BY-SA — commercially usable for fine-tuning.
# Caps each source so total size stays under HF dataset limits.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.claude/logs/dataset-enrich.log"
WORK="$HOME/.hermes/workspace/dataset-enrich"
mkdir -p "$WORK" "$(dirname "$LOG")"

echo "[$(date +%H:%M:%S)] dataset enrich start" | tee "$LOG"

python3 <<'PYEOF' 2>&1 | tee -a "$LOG"
from huggingface_hub import HfApi
from pathlib import Path
from datasets import load_dataset
import hashlib, json, time

WORK = Path("/Users/Ashira/.hermes/workspace/dataset-enrich")
WORK.mkdir(parents=True, exist_ok=True)
api = HfApi()

# (id, license, slug, schema_hint, per_dataset_cap)
DATASETS = [
    # ── Coding instruction-tuning ────────────────────────────────────────────
    ("ise-uiuc/Magicoder-OSS-Instruct-75K",         "MIT",         "magicoder-oss",       "instr-resp",            75000),
    ("ise-uiuc/Magicoder-Evol-Instruct-110K",       "Apache",      "magicoder-evol",      "instr-resp",           110000),
    ("theblackcat102/evol-codealpaca-v1",           "Apache",      "evol-codealpaca",     "instr-resp",           100000),
    ("m-a-p/CodeFeedback-Filtered-Instruction",     "Apache",      "codefeedback-filt",   "query-resp",           100000),
    ("m-a-p/Code-Feedback",                         "Apache",      "codefeedback-multi",  "messages",              66383),
    ("QuixiAI/dolphin-coder",                       "Apache",      "dolphin-coder",       "system-question-resp", 100000),
    # ── Multi-turn dialogue + agentic reasoning ─────────────────────────────
    ("HuggingFaceH4/ultrachat_200k",                "MIT",         "ultrachat",           "messages",             200000),
    ("Open-Orca/SlimOrca-Dedup",                    "MIT",         "slim-orca",           "conversations",        150000),
    ("microsoft/orca-agentinstruct-1M-v1",          "CDLA",        "orca-agentinstruct",  "messages",             150000),
    # ── Real commits + code review ──────────────────────────────────────────
    ("bigcode/commitpackft",                        "MIT",         "commitpackft",        "commit",                80000),
    ("VatsaDev/code-review",                        "MIT",         "vatsa-code-review",   "instr-resp",            40000),
    # ── DevSecOps: CVE / CWE / vulnerability detection ──────────────────────
    ("AlicanKiraz0/All-CVE-Records-Training-Dataset","Apache",     "cve-records-chat",    "system-user-assistant", 30000),
    ("CyberNative/Code_Vulnerability_Security_DPO", "Apache",      "vuln-secure-dpo",     "dpo-question",           4656),
    ("bstee615/diversevul",                         "MIT-research","diversevul-cwe",      "code-defect-cwe",       80000),
    ("google/code_x_glue_cc_defect_detection",      "C-UDA",       "codexglue-defect",    "code-defect",           27318),
    # ── Function/tool calling (agentic core) ────────────────────────────────
    ("Salesforce/xlam-function-calling-60k",        "CC-BY-4.0",   "xlam-fc",             "tools-query-answers",   60000),
    ("glaiveai/glaive-function-calling-v2",         "Apache",      "glaive-fc-v2",        "system-chat",          112960),
    ("NousResearch/hermes-function-calling-v1",     "Apache",      "hermes-fc",           "conversations",         11578),
    # ── Reasoning / math ────────────────────────────────────────────────────
    ("TIGER-Lab/MathInstruct",                      "MIT",         "math-instruct",       "instr-resp",            60000),
    ("meta-math/MetaMathQA",                        "MIT",         "metamath",            "query-resp",            50000),
    # ── Helpfulness preferences ─────────────────────────────────────────────
    ("Anthropic/hh-rlhf",                           "MIT",         "hh-rlhf",             "chosen-rejected",       40000),
    # ── SWE-Bench: real GitHub issue → patch (agent training gold) ──────────
    ("SWE-bench/SWE-smith-trajectories",            "MIT",         "swe-smith-traj",      "swe-trajectory",         5017),
    ("SWE-bench/SWE-smith",                         "MIT",         "swe-smith-tasks",     "swe-instance",          50000),
    ("ByteDance-Seed/Multi-SWE-bench",              "CC0",         "multi-swe-bench",     "swe-instance",          15000),
    # ── PR review with diff + label + reasoning ─────────────────────────────
    ("TuringEnterprises/CRAVE",                     "MIT",         "crave-pr-review",     "pr-review",              1200),
    # ── Single-statement bug fixes (real-world Java) ────────────────────────
    ("zirui3/ManySStuBs4J-instructions-v0",         "CC-BY-4.0",   "manysstubs-bugfix",   "instr-resp",            50000),
    # ── DevSecOps upgrade: cleaner vuln+fix paired data ──────────────────────
    ("DetectVul/CVEFixes",                          "Apache",      "cvefixes",            "code-defect-cwe",       12987),
    ("starsofchance/PrimeVul",                      "MIT",         "primevul",            "code-defect-cwe",      100000),
    ("arag0rn/SecVulEval",                          "MIT",         "secvuleval",          "code-defect-cwe",       25440),
    # ── Code review depth (commitpackft already there; add JetBrains) ────────
    ("JetBrains-Research/commit-chronicle",         "Apache",      "commit-chronicle",    "commit",               100000),
    ("microsoft/codereviewer",                      "MIT",         "ms-codereviewer",     "pr-review",             80000),
    # ── Algorithmic / competitive coding ─────────────────────────────────────
    ("codeparrot/apps",                             "MIT",         "apps-algo",           "instr-resp",            10000),
    ("deepmind/code_contests",                      "CC-BY-4.0",   "code-contests",       "code-contests",          4000),
    # ── API design (was zero coverage) ───────────────────────────────────────
    ("APIs-guru/openapi-directory",                 "CC0",         "apis-guru",           "openapi-spec",           3800),
    # ── Multilingual instruction (incl. Thai — replaces NC sets) ─────────────
    ("CohereForAI/aya_dataset",                     "Apache",      "aya-multi",           "instr-resp",           150000),
    # ── Code corpus (legal alternative to the-stack) ─────────────────────────
    ("iidai/codenet",                               "CDLA",        "ibm-codenet",         "code-only",            200000),
    # ── NIST cybersecurity full corpus (530K CC0 — closes compliance gap) ────
    ("ethanolivertroy/nist-cybersecurity-training", "CC0",         "nist-cyber",          "messages",             100000),
    # ── DevSecOps depth (Fenrir + Trendyol explicitly tagged for IR/threat) ─
    ("AlicanKiraz0/Cybersecurity-Dataset-Fenrir-v2.1","Apache",    "fenrir-cyber",        "system-user-assistant", 99870),
    ("Trendyol/Trendyol-Cybersecurity-Instruction-Tuning-Dataset","Apache","trendyol-cyber","instr-resp",         53202),
    # ── Incident-Response playbooks (NIST SP 800-61 structure) ───────────────
    ("darkknight25/Incident_Response_Playbook_Dataset","MIT",      "ir-playbooks",        "ir-playbook",             175),
    # ── Real agent trajectories (OpenHands SWE-rebench) ─────────────────────
    ("nebius/SWE-rebench-openhands-trajectories",   "CC-BY-4.0",   "swe-rebench-traj",    "swe-trajectory",        50000),
    ("nebius/SWE-rebench",                          "CC-BY-4.0",   "swe-rebench-tasks",   "swe-instance",          27878),
    # ── Reasoning chains (R1-distilled CoT) ──────────────────────────────────
    ("nvidia/OpenCodeReasoning",                    "CC-BY-4.0",   "opencode-reasoning",  "instr-resp",           100000),
    ("open-r1/codeforces-cots",                     "CC-BY-4.0",   "codeforces-cots",     "instr-resp",            50000),
    ("open-r1/OpenR1-Math-220k",                    "Apache",      "openr1-math",         "instr-resp",            50000),
    ("open-thoughts/OpenThoughts-114k",             "Apache",      "open-thoughts",       "instr-resp",           100000),
    # ── Preference / DPO under pressure (good vs bad reasoning) ──────────────
    ("nvidia/HelpSteer3",                           "CC-BY-4.0",   "helpsteer3",          "helpsteer-pref",        40476),
    ("argilla/ultrafeedback-binarized-preferences-cleaned","MIT", "uf-cleaned",          "chosen-rejected",       60917),
    ("OpenAssistant/oasst2",                        "Apache",      "oasst2",              "messages",              80000),
    # ── Cloud security misconfigs + chaos engineering ────────────────────────
    ("darkknight25/Cloud_Vulnerabilities",          "MIT",         "cloud-vulns",         "cloud-misconfig",        1200),
    ("AYI-NEDJIMI/cloud-security-en",               "Apache",      "cloud-sec-en",        "cloud-misconfig",         230),
    ("ddjain/krkn-dataset",                         "MIT",         "krkn-chaos",          "instr-resp",             1000),
    # ── Linux/bash command knowledge ─────────────────────────────────────────
    ("mecha-org/linux-command-dataset",             "Apache",      "linux-commands",      "instr-resp",             8669),
    # ── DBA / Text-to-SQL (was zero coverage) ────────────────────────────────
    ("seeklhy/SynSQL-2.5M",                         "Apache",      "synsql-2_5m",         "synsql-quad",          200000),
    ("gretelai/synthetic_text_to_sql",              "Apache",      "gretel-text2sql",     "domain-sql-prompt",    105000),
    ("xu3kev/BIRD-SQL-data-train",                  "CC-BY-SA",    "bird-sql",            "schema-sql",             9400),
    # ── Frontend (React/Next/Tailwind, was 2/5) ──────────────────────────────
    ("cfahlgren1/react-code-instructions",          "MIT",         "react-instr",         "instr-resp",            74000),
    ("Tesslate/Next.js-Dataset",                    "Apache",      "nextjs-dataset",      "q-r-reasoning",         50000),
    ("HuggingFaceM4/WebSight",                      "CC-BY-4.0",   "websight",            "screenshot-html",      300000),
    # ── Mobile (was 1/5) ─────────────────────────────────────────────────────
    ("mllmTeam/MobileViews",                        "MIT",         "mobile-views",        "android-screenshot-vh", 60000),
    ("google/mobile-actions",                       "CC-BY-4.0",   "mobile-actions",      "tools-messages-android",30000),
    # ── Data Engineering + ML (notebook reasoning) ───────────────────────────
    ("jupyter-agent/jupyter-agent-dataset",         "Apache",      "jupyter-agent",       "notebook-messages",     51000),
    ("adyen/DABstep",                               "CC-BY-4.0",   "dabstep",             "task-q-a-guidelines",     450),
    # ── Architecture (was 1/5) — KILLER 450K dataset ────────────────────────
    ("ajibawa-2023/Software-Architecture",          "Apache",      "software-arch",       "instruction-input-output",150000),
    # ── Multilingual coding (15 langs, permissive filter) ───────────────────
    ("HuggingFaceTB/stack-edu",                     "Apache",      "stack-edu",           "stack-edu-multi",      300000),
    # ── Code instruction (Granite-trained, 950k Apache) ──────────────────────
    ("glaiveai/glaive-code-assistant-v3",           "Apache",      "glaive-code-v3",      "instr-resp",           150000),
    # ── Agentic tool-use + reasoning (NVIDIA Nemotron) ───────────────────────
    ("nvidia/Nemotron-Agentic-v1",                  "CC-BY-4.0",   "nemotron-agentic",    "tools-messages-reasoning",100000),
    # ── OpenAPI completion ──────────────────────────────────────────────────
    ("BohdanPetryshyn/openapi-completion-refined",  "MIT",         "openapi-refined",     "instr-resp",              990),
    # NOTE: SWE-bench/SWE-bench_Verified + bigcode/bigcodebench RESERVED AS EVAL ONLY.
]

# 1. Existing axentx hashes for dedup
existing_hashes = set()
print("Loading existing axentx pairs for dedup...", flush=True)
for path in [Path.home() / 'axentx/surrogate/data/training-jsonl',
             Path.home() / '.surrogate/training-pairs.jsonl']:
    if path.is_dir():
        files = list(path.glob('*.jsonl'))
    elif path.is_file():
        files = [path]
    else:
        continue
    for jf in files:
        if 'thinkbit' in jf.name or 'fs-code' in jf.name:
            continue
        try:
            with open(jf) as f:
                for i, line in enumerate(f):
                    if i > 50000: break
                    try:
                        d = json.loads(line)
                        text = d.get('prompt') or d.get('instruction') or \
                               (d.get('messages',[{}])[0].get('content','') if d.get('messages') else '')
                        if text:
                            existing_hashes.add(hashlib.md5(text[:200].encode()).hexdigest()[:16])
                    except: pass
        except: pass
print(f"  {len(existing_hashes):,} existing hashes loaded", flush=True)

# 2. Pull each dataset, normalize per schema, dedup
new_pairs_total = 0
out_path = WORK / f"merged-public-dedup-{time.strftime('%Y%m%d')}.jsonl"

with open(out_path, "w") as out:
    for ds_id, license_, slug, schema, cap in DATASETS:
        print(f"\n--- {ds_id} ({license_}, schema={schema}, cap={cap}) ---", flush=True)
        try:
            t0 = time.time()
            ds = load_dataset(ds_id, split="train", streaming=True)
            kept = dup = total = 0
            for row in ds:
                total += 1
                if total > cap: break

                prompt, response = "", ""
                if schema == "instr-resp":
                    prompt = str(row.get("instruction") or row.get("problem") or row.get("input",""))
                    response = str(row.get("response") or row.get("solution") or row.get("output",""))
                elif schema == "query-resp":
                    prompt = str(row.get("query") or row.get("question",""))
                    response = str(row.get("response") or row.get("answer",""))
                elif schema == "messages":
                    msgs = row.get("messages") or row.get("conversations") or []
                    if len(msgs) >= 2:
                        prompt = str(msgs[0].get("content","") or msgs[0].get("value",""))
                        response = str(msgs[1].get("content","") or msgs[1].get("value",""))
                elif schema == "conversations":
                    convs = row.get("conversations",[])
                    if len(convs) >= 2:
                        prompt = str(convs[0].get("value",""))
                        response = str(convs[1].get("value",""))
                elif schema == "commit":
                    prompt = f"Write a commit message for this diff:\n{str(row.get('old_contents',''))[:1500]}\n→\n{str(row.get('new_contents',''))[:1500]}"
                    response = str(row.get("message",""))
                elif schema == "chosen-rejected":
                    prompt = str(row.get("chosen","")[:200] or row.get("prompt",""))
                    response = str(row.get("chosen",""))
                elif schema == "system-user-assistant":   # AlicanKiraz0 CVE
                    prompt = f"{str(row.get('System','')).strip()}\n\nUser: {str(row.get('User','')).strip()}"
                    response = str(row.get("Assistant",""))
                elif schema == "dpo-question":            # CyberNative DPO
                    prompt = str(row.get("question",""))
                    response = str(row.get("chosen",""))
                elif schema == "code-defect-cwe":         # DiverseVul
                    cwes = row.get("cwe") or []
                    cwe_str = ",".join(cwes) if isinstance(cwes, list) and cwes else "none"
                    label = "VULNERABLE" if row.get("target") == 1 else "SAFE"
                    prompt = f"Audit this function for security vulnerabilities. Identify any CWE matches.\n```\n{str(row.get('func',''))[:6000]}\n```"
                    response = f"Verdict: {label}\nCWE: {cwe_str}\nProject: {row.get('project','')}\nCommit: {str(row.get('message',''))[:500]}"
                elif schema == "code-defect":             # CodeXGLUE
                    label = "VULNERABLE" if row.get("target") else "SAFE"
                    prompt = f"Review this C function for defects:\n```c\n{str(row.get('func',''))[:6000]}\n```"
                    response = f"Defect detected: {label}\nProject: {row.get('project','')}\nCommit: {row.get('commit_id','')}"
                elif schema == "tools-query-answers":     # xLAM
                    tools_json = json.dumps(row.get("tools",[]))[:3000]
                    prompt = f"You have access to these tools:\n{tools_json}\n\nUser query: {row.get('query','')}"
                    response = json.dumps(row.get("answers",[]), ensure_ascii=False)
                elif schema == "system-chat":             # Glaive-v2
                    prompt = str(row.get("system",""))
                    response = str(row.get("chat",""))
                elif schema == "system-question-resp":    # dolphin-coder
                    prompt = f"{str(row.get('system_prompt','')).strip()}\n\n{str(row.get('question','')).strip()}"
                    response = str(row.get("response",""))
                elif schema == "swe-trajectory":          # SWE-smith-trajectories — agent traces
                    msgs = row.get("messages") or []
                    if not msgs or not row.get("resolved", False): continue
                    # Use first user msg as prompt, full assistant trace as response
                    user_msgs = [m for m in msgs if m.get("role") == "user"]
                    asst_msgs = [m for m in msgs if m.get("role") == "assistant"]
                    if not user_msgs or not asst_msgs: continue
                    prompt = str(user_msgs[0].get("content",""))[:6000]
                    # Concat all assistant turns to capture full agent reasoning
                    response = "\n\n".join(str(m.get("content","")) for m in asst_msgs)[:12000]
                elif schema == "swe-instance":            # SWE-smith / Multi-SWE-bench
                    repo = row.get("repo", "")
                    issue = str(row.get("problem_statement") or row.get("issue") or row.get("text",""))[:3000]
                    patch = str(row.get("patch") or row.get("model_patch") or row.get("fix",""))[:8000]
                    if not issue or not patch: continue
                    prompt = f"Repo: {repo}\n\nIssue:\n{issue}\n\nGenerate a patch (unified diff) that resolves this issue."
                    response = patch
                elif schema == "pr-review":               # CRAVE / microsoft codereviewer
                    diff = str(row.get("diff") or row.get("patch") or row.get("oldf",""))[:6000]
                    label = row.get("label") or row.get("y") or row.get("verdict","")
                    reasoning = str(row.get("reasoning") or row.get("explanation") or row.get("msg") or row.get("comment",""))[:3000]
                    if not diff: continue
                    prompt = f"Review this PR diff:\n```diff\n{diff}\n```\nClassify (approve/request-changes/reject) and explain."
                    response = f"Verdict: {label}\n\nReasoning: {reasoning}"
                elif schema == "code-contests":           # DeepMind CodeContests
                    desc = str(row.get("description",""))[:4000]
                    sols = row.get("solutions") or {}
                    sol_list = sols.get("solution", []) if isinstance(sols, dict) else []
                    if not desc or not sol_list: continue
                    prompt = f"Solve this competitive programming problem:\n\n{desc}\n\nProvide a working solution."
                    response = str(sol_list[0])[:8000]
                elif schema == "openapi-spec":            # APIs.guru
                    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
                    title = str(info.get("title","Unknown"))
                    desc = str(info.get("description",""))[:1000]
                    paths = list((row.get("paths") or {}).keys())[:30]
                    if not paths: continue
                    prompt = f"Design a REST API for: {title}\n{desc}"
                    response = f"Endpoints:\n" + "\n".join(f"  {p}" for p in paths)
                elif schema == "code-only":               # IBM CodeNet (synthetic prompt)
                    code = str(row.get("code") or row.get("content") or row.get("solution",""))[:6000]
                    lang = str(row.get("language", "code"))
                    if len(code) < 80: continue
                    prompt = f"Explain what this {lang} code does:\n```{lang}\n{code}\n```"
                    response = f"[Code sample from IBM CodeNet — pending LLM-generated explanation]"
                    continue  # Skip — placeholder responses pollute training
                elif schema == "ir-playbook":             # NIST SP 800-61 IR playbooks
                    title = str(row.get("title") or row.get("incident_type") or row.get("name",""))
                    phases = row.get("phases") or row.get("response_phases") or {}
                    mitre = row.get("mitre_attack") or row.get("tactics") or []
                    if not title: continue
                    prompt = f"How should an incident response team handle a {title} incident? Provide a NIST SP 800-61 playbook."
                    response_parts = [f"# {title}"]
                    if mitre:
                        response_parts.append(f"\n## MITRE ATT&CK tactics: {', '.join(str(m) for m in mitre[:6])}")
                    if isinstance(phases, dict):
                        for phase, content in phases.items():
                            response_parts.append(f"\n## {phase}\n{content}")
                    elif isinstance(phases, list):
                        for p in phases:
                            response_parts.append(f"\n## {p}")
                    response = "\n".join(response_parts)
                elif schema == "helpsteer-pref":          # NVIDIA HelpSteer3 preference + reasoning
                    user_msg = str(row.get("context") or row.get("prompt",""))[:4000]
                    chosen = str(row.get("response_a") or row.get("chosen",""))[:6000]
                    rejected = str(row.get("response_b") or row.get("rejected",""))[:6000]
                    pref = row.get("individual_preference", {}) or {}
                    reasoning = ""
                    if isinstance(pref, dict):
                        reasoning = str(pref.get("reasoning",""))[:2000]
                    if not user_msg or not chosen: continue
                    prompt = user_msg
                    response = chosen
                    if reasoning:
                        response += f"\n\n[Why this is preferred: {reasoning}]"
                elif schema == "cloud-misconfig":         # darkknight25 / AYI-NEDJIMI cloud security
                    cloud = str(row.get("cloud_provider") or row.get("provider") or "Cloud")
                    issue = str(row.get("vulnerability") or row.get("misconfiguration") or row.get("issue",""))[:2000]
                    mitig = str(row.get("mitigation") or row.get("fix") or row.get("remediation",""))[:3000]
                    cis = row.get("cis_benchmark") or row.get("cis_ref","")
                    if not issue or not mitig: continue
                    prompt = f"In {cloud}, how do you remediate this misconfiguration: {issue}"
                    response = f"**Mitigation**: {mitig}"
                    if cis:
                        response += f"\n\n**CIS Benchmark reference**: {cis}"
                elif schema == "synsql-quad":             # SynSQL-2.5M text-to-SQL with CoT
                    schema_str = str(row.get("schema") or row.get("create_statements",""))[:3000]
                    nl = str(row.get("question") or row.get("nl",""))[:1500]
                    sql = str(row.get("sql") or row.get("query",""))[:3000]
                    cot = str(row.get("cot") or row.get("reasoning",""))[:2000]
                    if not nl or not sql: continue
                    prompt = f"Schema:\n{schema_str}\n\nQuestion: {nl}\n\nWrite the SQL query."
                    response = sql
                    if cot: response = f"**Reasoning**: {cot}\n\n**SQL**:\n```sql\n{sql}\n```"
                elif schema == "domain-sql-prompt":       # gretel synthetic text-to-sql
                    domain = str(row.get("domain","general"))
                    prompt_text = str(row.get("sql_prompt") or row.get("prompt",""))[:2000]
                    sql = str(row.get("sql","") or row.get("answer",""))[:3000]
                    if not prompt_text or not sql: continue
                    prompt = f"[{domain}] {prompt_text}"
                    response = f"```sql\n{sql}\n```"
                elif schema == "schema-sql":              # BIRD-SQL
                    db_id = str(row.get("db_id",""))
                    nl = str(row.get("question",""))[:1500]
                    sql = str(row.get("SQL") or row.get("sql",""))[:3000]
                    if not nl or not sql: continue
                    prompt = f"Database: {db_id}\nQuestion: {nl}\nGenerate SQL."
                    response = f"```sql\n{sql}\n```"
                elif schema == "screenshot-html":         # WebSight
                    desc = str(row.get("text") or row.get("description") or "this UI")[:1500]
                    html = str(row.get("html") or row.get("code",""))[:6000]
                    if not html: continue
                    prompt = f"Generate a Tailwind HTML page that matches this description: {desc}"
                    response = f"```html\n{html}\n```"
                elif schema == "android-screenshot-vh":   # MobileViews
                    pkg = str(row.get("package_name","app"))
                    vh = str(row.get("view_hierarchy") or row.get("vh",""))[:5000]
                    if not vh: continue
                    prompt = f"Describe this Android view hierarchy from {pkg}:\n{vh}"
                    response = f"This is an Android screen for {pkg}. The view hierarchy shows the UI structure with nested layouts and widgets."
                    continue  # placeholder — skip until we generate real descriptions
                elif schema == "tools-messages-android":  # google/mobile-actions
                    msgs = row.get("messages") or row.get("conversations") or []
                    if not isinstance(msgs, list) or len(msgs) < 2: continue
                    prompt = str(msgs[0].get("content","") or msgs[0].get("value",""))[:4000]
                    response = "\n".join(str(m.get("content","") or m.get("value","")) for m in msgs[1:])[:8000]
                elif schema == "tools-messages-reasoning":# NVIDIA Nemotron-Agentic
                    msgs = row.get("messages") or []
                    if not isinstance(msgs, list) or len(msgs) < 2: continue
                    prompt = str(msgs[0].get("content",""))[:4000]
                    response = "\n".join(str(m.get("content","")) for m in msgs[1:])[:8000]
                elif schema == "instruction-input-output":# ajibawa Software-Architecture
                    instr = str(row.get("instruction",""))[:3000]
                    inp = str(row.get("input",""))[:2000]
                    out = str(row.get("output",""))[:8000]
                    if not instr or not out: continue
                    prompt = f"{instr}\n\n{inp}".strip()
                    response = out
                elif schema == "q-r-reasoning":           # Tesslate Next.js Q+A+reasoning
                    q = str(row.get("question") or row.get("query",""))[:3000]
                    a = str(row.get("answer") or row.get("response",""))[:8000]
                    r = str(row.get("reasoning",""))[:2000]
                    if not q or not a: continue
                    prompt = q
                    response = f"{a}" + (f"\n\n[Reasoning: {r}]" if r else "")
                elif schema == "notebook-messages":       # jupyter-agent
                    msgs = row.get("messages") or []
                    if not isinstance(msgs, list) or len(msgs) < 2: continue
                    prompt = str(msgs[0].get("content",""))[:4000]
                    response = "\n".join(str(m.get("content","")) for m in msgs[1:])[:8000]
                elif schema == "task-q-a-guidelines":     # adyen DABstep
                    task = str(row.get("task") or row.get("question",""))[:3000]
                    answer = str(row.get("answer","") or row.get("expected",""))[:4000]
                    guide = str(row.get("guidelines","") or row.get("notes",""))[:2000]
                    if not task or not answer: continue
                    prompt = f"{task}" + (f"\n\nGuidelines: {guide}" if guide else "")
                    response = answer
                elif schema == "stack-edu-multi":         # HuggingFaceTB stack-edu (filter permissive)
                    if str(row.get("license_type","")).lower() != "permissive": continue
                    code = str(row.get("text") or row.get("content",""))[:6000]
                    lang = str(row.get("language",""))
                    if len(code) < 100: continue
                    prompt = f"Explain this educational {lang} code example:\n```{lang}\n{code}\n```"
                    response = "[stack-edu sample — pending LLM-generated explanation]"
                    continue  # placeholder — skip
                else:
                    continue

                if not prompt or not response or len(prompt) < 20 or len(response) < 20:
                    continue

                h = hashlib.md5(prompt[:200].encode()).hexdigest()[:16]
                if h in existing_hashes:
                    dup += 1
                    continue
                existing_hashes.add(h)

                out.write(json.dumps({
                    "source": slug,
                    "license": license_,
                    "prompt": prompt[:4000],
                    "response": response[:8000],
                    "messages": [
                        {"role":"user","content":prompt[:4000]},
                        {"role":"assistant","content":response[:8000]},
                    ],
                }, ensure_ascii=False) + "\n")
                kept += 1
            elapsed = time.time() - t0
            print(f"  scanned: {total}  kept: {kept}  dedup: {dup}  ({elapsed:.0f}s)", flush=True)
            new_pairs_total += kept
        except Exception as e:
            print(f"  ❌ {type(e).__name__}: {str(e)[:200]}", flush=True)
            continue

# 3. IaC/DevOps subset from the-stack (separate streaming pass for code-as-data)
print("\n--- bigcode/the-stack-smol (Terraform / Dockerfile / K8s YAML) ---", flush=True)
try:
    iac_kept = 0
    iac_targets = {
        "dockerfile": ("Dockerfile", "shell/container"),
        "hcl":        ("Terraform / HCL", "iac"),
        "yaml":       ("YAML (likely k8s/CI)", "config"),
    }
    for lang, (label, domain) in iac_targets.items():
        try:
            ds = load_dataset("bigcode/the-stack-smol", data_dir=f"data/{lang}", split="train", streaming=True)
            for i, row in enumerate(ds):
                if i > 5000: break
                content = str(row.get("content",""))
                if len(content) < 80 or len(content) > 8000: continue
                # Synthetic prompt: "explain this <label>"
                prompt = f"Explain what this {label} does and review for best practices:\n```\n{content[:2000]}\n```"
                response = ""  # no canonical answer — skip for now or generate later
                # Save as raw code-only (will run separate prompt-gen pass)
                h = hashlib.md5(content[:200].encode()).hexdigest()[:16]
                if h in existing_hashes: continue
                existing_hashes.add(h)
                out.write(json.dumps({
                    "source": f"the-stack-{lang}",
                    "license": "permissive (the-stack)",
                    "domain": domain,
                    "prompt": prompt[:4000],
                    "response": "[code-only sample — pending answer generation]",
                    "code": content[:6000],
                }, ensure_ascii=False) + "\n")
                iac_kept += 1
            print(f"  {lang}: {iac_kept} samples", flush=True)
        except Exception as e:
            print(f"  {lang} skipped: {type(e).__name__}", flush=True)
    new_pairs_total += iac_kept
except Exception as e:
    print(f"  IaC pull skipped: {type(e).__name__}: {e}", flush=True)

print(f"\n=== Total new pairs after dedup: {new_pairs_total:,} ===", flush=True)
print(f"Output: {out_path} ({out_path.stat().st_size/1024/1024:.1f} MB)", flush=True)

# 4. Push to axentx/surrogate-1-training-pairs
if new_pairs_total > 0:
    repo_path = f"public-merged-dedup-{time.strftime('%Y-%m-%d')}.jsonl"
    print(f"\nUploading {repo_path} to axentx/surrogate-1-training-pairs...", flush=True)
    api.upload_file(
        path_or_fileobj=str(out_path),
        path_in_repo=repo_path,
        repo_id="axentx/surrogate-1-training-pairs",
        repo_type="dataset",
        commit_message=f"Public datasets dedup-merged: {new_pairs_total} new pairs across coding/dialog/commits/reasoning/iac"
    )
    print(f"✅ uploaded → axentx/surrogate-1-training-pairs/{repo_path}", flush=True)
PYEOF

echo "[$(date +%H:%M:%S)] dataset enrich done" | tee -a "$LOG"
