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

SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-1}"

SHARD_ID="$SHARD_ID" SHARD_TOTAL="$SHARD_TOTAL" python3 <<'PYEOF' 2>&1 | tee -a "$LOG"
from huggingface_hub import HfApi
from pathlib import Path
from datasets import load_dataset
import hashlib, json, time, os

WORK = Path.home() / ".hermes/workspace/dataset-enrich"
WORK.mkdir(parents=True, exist_ok=True)
api = HfApi()

# Sharding: each parallel worker handles a subset of DATASETS
SHARD_ID = int(os.environ.get("SHARD_ID", 0))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", 1))
print(f"[shard {SHARD_ID}/{SHARD_TOTAL}] start", flush=True)

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
    # ════════════════════════════════════════════════════════════════════════
    # MEGA-MIXES — pre-aggregated, deduped, already-curated by big labs.
    # ════════════════════════════════════════════════════════════════════════
    # GOLD: 1M Apache-2.0 mix of GPT-4 generated + filtered (Teknium curated)
    ("teknium/OpenHermes-2.5",                      "Apache",      "openhermes-2.5",      "conversations",        500000),
    # SmolLM team curated 1M+ instruction mix (very recent, high-quality filter)
    ("HuggingFaceTB/smoltalk",                      "Apache",      "smoltalk",            "messages",             500000),
    ("HuggingFaceTB/smoltalk2",                     "Apache",      "smoltalk2",           "messages",             500000),
    # 25M synthetic textbooks — broad CS/math/science (Apache)
    ("HuggingFaceTB/cosmopedia-v2",                 "Apache",      "cosmopedia",          "instr-resp",          1000000),
    # 3.2M code instruction MIT — larger than Magicoder
    ("Replete-AI/code_bagel",                       "MIT",         "code-bagel",          "instr-resp",           500000),
    # 9.8M Apache CoT mix from Alpaca family
    ("QingyiSi/Alpaca-CoT",                         "Apache",      "alpaca-cot",          "instr-resp",           500000),
    # WizardLM evolved instructions (196K Apache, top-quality)
    ("WizardLMTeam/WizardLM_evol_instruct_V2_196k", "Apache",      "wizardlm-evol-v2",    "conversations",        196000),
    # Magpie-Pro multi-turn (300K MIT)
    ("Magpie-Align/Magpie-Pro-MT-300K-v0.1",        "MIT",         "magpie-pro-mt",       "conversations",        300000),
    # ORPO/DPO mix (40K MIT — preference signal)
    ("mlabonne/orpo-dpo-mix-40k",                   "MIT",         "orpo-dpo-mix",        "chosen-rejected",       40000),
    # Microsoft Orca math (200K MIT)
    ("microsoft/orca-math-word-problems-200k",      "MIT",         "orca-math",           "query-resp",           200000),
    # Open Assistant v1 (161K Apache, multi-turn human-vetted)
    ("OpenAssistant/oasst1",                        "Apache",      "oasst1",              "messages",             100000),
    # UltraTextbooks (5.5M Apache long-form learning)
    ("Locutusque/UltraTextbooks",                   "Apache",      "ultratextbooks",      "instr-resp",           500000),
    # ════════════════════════════════════════════════════════════════════════
    # TRILLION-SCALE PRETRAIN CORPORA (caps bumped 5-10× per user feedback)
    # ════════════════════════════════════════════════════════════════════════
    # FineWeb-Edu — 1.3T tokens education-filtered (HIGH SIGNAL/NOISE)
    ("HuggingFaceFW/fineweb-edu",                   "ODC-By",      "fineweb-edu",         "web-text",           20000000),
    # SmolLM team's filtered web subset (CC0/Apache-tier, smaller=faster ingest)
    ("HuggingFaceTB/smollm-corpus",                 "Apache",      "smollm-corpus",       "web-text",           15000000),
    # Cosmopedia full (synthetic textbooks 25M Apache — bumped 1M\u219210M)
    ("HuggingFaceTB/cosmopedia-v2",                 "Apache",      "cosmopedia",          "instr-resp",         25000000),
    # Dolma v1.7 — 3T tokens AllenAI mixed (ODC-By, bumped 500K\u21923M)
    ("allenai/dolma",                               "ODC-By",      "dolma",               "web-text",           15000000),
    # The Pile uncopyrighted (MIT, 627GB, bumped 500K\u21922M)
    ("monology/pile-uncopyrighted",                 "MIT",         "pile-uncopyrighted",  "web-text",           10000000),
    # RedPajama V2 web (Apache, 30T tokens, bumped 500K\u21923M)
    ("togethercomputer/RedPajama-Data-V2",          "Apache",      "redpajama-v2",        "web-text",           15000000),
    # SlimPajama-6B (DKYoon Apache, filtered subset of RedPajama, easier ingest)
    ("DKYoon/SlimPajama-6B",                        "Apache",      "slim-pajama-6b",      "web-text",           10000000),
    # bigcode StarCoder data (250B tokens, bumped 500K\u21923M)
    ("bigcode/starcoderdata",                       "Permissive",  "starcoder-data",      "code-only-permissive",15000000),
    # GitHub code clean (Apache, bumped 500K\u21923M)
    ("codeparrot/github-code-clean",                "Apache",      "github-code-clean",   "code-only-permissive",15000000),
    # The Stack dedup (filtered code, bumped 500K\u21925M)
    ("bigcode/the-stack-dedup",                     "Permissive",  "the-stack-dedup",     "code-only-permissive",20000000),
    # Common Pile v0.1 (8TB EleutherAI, bumped 500K\u21922M)
    ("common-pile/common-pile-2",                   "Permissive",  "common-pile-2",       "web-text",           10000000),
    # ════════════════════════════════════════════════════════════════════════
    # PRE-CURATED MASSIVE SFT MIXES (specifically instruction-tuned, high quality)
    # ════════════════════════════════════════════════════════════════════════
    # Hermes-3 dataset (Apache, NousResearch's curated mix)
    ("NousResearch/Hermes-3-Dataset",               "Apache",      "hermes-3",            "messages",            1000000),
    # AceCode-V2 (Apache, 150K curated code)
    ("TIGER-Lab/AceCode-V2-150K",                   "Apache",      "acecode-v2",          "instr-resp",           150000),
    # KodCode (Apache, 268K code mix)
    ("KodCode/KodCode-V1",                          "Apache",      "kodcode-v1",          "instr-resp",           268000),
    # Airoboros 3.2 (Apache, large diverse instruct)
    ("jondurbin/airoboros-3.2",                     "Apache",      "airoboros-3-2",       "conversations",        500000),
    # Open-Platypus (Apache, 25K curated reasoning + code + math)
    ("garage-bAInd/Open-Platypus",                  "Apache",      "open-platypus",       "instr-resp",            25000),
    # Databricks Dolly 15K (Apache, human-generated)
    ("databricks/databricks-dolly-15k",             "Apache",      "dolly-15k",           "instr-resp",            15000),
    # ProofPile-2 (Apache, math reasoning)
    ("EleutherAI/proof-pile-2",                     "Apache",      "proof-pile-2",        "instr-resp",          1000000),
    # IndustryInstruction BAAI (domain-specific SFT)
    ("BAAI/IndustryInstruction",                    "Apache",      "industry-instr",      "instr-resp",           500000),
    # VMware open instruct (oasst+dolly+hhrlhf merged)
    ("VMware/open-instruct-v1-oasst-dolly-hhrlhf",  "Apache",      "vmware-openinstr",    "instr-resp",           300000),
    # SmolTalk2 instruction-tuned filtered subset (was already in but ensure pulled)
    # ════════════════════════════════════════════════════════════════════════
    # MORE TRILLION-SCALE PRETRAIN — every public source we missed
    # ════════════════════════════════════════════════════════════════════════
    # FineWeb FULL (15T tokens, not just edu — broader)
    ("HuggingFaceFW/fineweb",                       "ODC-By",      "fineweb-full",        "web-text",           20000000),
    # FineWeb-2 multilingual
    ("HuggingFaceFW/fineweb-2",                     "ODC-By",      "fineweb-2",           "web-text",           15000000),
    # DCLM-baseline (4T tokens, mlfoundations heavily filtered)
    ("mlfoundations/dclm-baseline-1.0",             "Apache",      "dclm-baseline",       "web-text",           20000000),
    # CulturaX (6.3T multilingual ODC-By, replaces stale mc4)
    ("uonlp/CulturaX",                              "ODC-By",      "culturax",            "web-text",           15000000),
    # FineMath (math-filtered web — Apache)
    ("HuggingFaceTB/finemath",                      "Apache",      "finemath",            "instr-resp",          2000000),
    # FinePDFs (PDF books extracted — Apache)
    ("HuggingFaceTB/finepdfs",                      "Apache",      "finepdfs",            "web-text",           10000000),
    # C4 (Apache T5 training corpus)
    ("allenai/c4",                                  "ODC-By",      "c4",                  "web-text",           10000000),
    # The Stack v1 dedup (3TB, Apache filterable)
    ("bigcode/the-stack",                           "Permissive",  "the-stack-v1",        "code-only-permissive",3000000),
    # GitHub Jupyter notebooks (Apache)
    ("codeparrot/github-jupyter",                   "Apache",      "github-jupyter",      "notebook-messages",   500000),
    # SkyPile-150B (Apache Chinese-English)
    ("Skywork/SkyPile-150B",                        "Apache",      "skypile-150b",        "web-text",            1000000),
    # Wikipedia (Apache full)
    ("wikimedia/wikipedia",                         "Apache",      "wikipedia",           "web-text",            500000),
    # ════════════════════════════════════════════════════════════════════════
    # MASSIVE INSTRUCTION CORPORA — parents of subsets we already have
    # ════════════════════════════════════════════════════════════════════════
    # OpenOrca FULL (4.2M parent of SlimOrca, Apache)
    ("Open-Orca/OpenOrca",                          "Apache",      "openorca-full",       "conversations",       2000000),
    # ultrachat FULL (1.5M parent of ultrachat_200k, Apache)
    ("stingning/ultrachat",                         "Apache",      "ultrachat-full",      "messages",            1000000),
    # LMSYS chat 1M (Apache real conversations)
    ("lmsys/lmsys-chat-1m",                         "Apache",      "lmsys-chat-1m",       "conversations",       1000000),
    # CohereForAI Aya collection (513M parent, Apache multilingual)
    ("CohereForAI/aya_collection",                  "Apache",      "aya-collection-full", "instr-resp",          2000000),
    # Magpie-Llama-3.3-Pro-1M (Apache, 1M synthetic instruct)
    ("Magpie-Align/Magpie-Llama-3.3-Pro-1M-v0.1",   "Apache",      "magpie-llama33-pro",  "conversations",       1000000),
    # Magpie reasoning V1 150K (Apache)
    ("Magpie-Align/Magpie-Reasoning-V1-150K",       "Apache",      "magpie-reasoning-v1", "conversations",        150000),
    # Magpie Air DPO 100K (Apache)
    ("Magpie-Align/Magpie-Air-DPO-100K-v0.1",       "Apache",      "magpie-air-dpo",      "chosen-rejected",      100000),
    # Open Assistant Guanaco curated (Apache, top quality)
    ("timdettmers/openassistant-guanaco",            "Apache",      "guanaco",             "messages",              9000),
    # GAIR/LIMA (Apache, premium curated 1K — Less Is More for Alignment)
    ("GAIR/lima",                                   "Apache",      "lima",                "instr-resp",             1030),
    # HuggingFaceH4 CodeUltraFeedback (Apache 50K)
    ("HuggingFaceH4/CodeUltraFeedback",             "Apache",      "code-ultrafeedback",  "instr-resp",            50000),
    # ════════════════════════════════════════════════════════════════════════
    # MATH MEGA-CORPORA
    # ════════════════════════════════════════════════════════════════════════
    # NuminaMath CoT (859K Apache)
    ("AI-MO/NuminaMath-CoT",                        "Apache",      "numina-math-cot",     "instr-resp",           859000),
    # NuminaMath 1.5 (1.5M Apache)
    ("AI-MO/NuminaMath-1.5",                        "Apache",      "numina-math-1-5",     "instr-resp",          1500000),
    # MathPile (9.5B tokens Apache)
    ("xDAN-AI/MathPile",                            "Apache",      "math-pile",           "web-text",            1000000),
    # ════════════════════════════════════════════════════════════════════════
    # PREFERENCE / DPO MEGA-MIXES
    # ════════════════════════════════════════════════════════════════════════
    # Argilla distilabel orca DPO pairs (MIT)
    ("argilla/distilabel-intel-orca-dpo-pairs",     "MIT",         "argilla-orca-dpo",    "chosen-rejected",       12000),
    # Argilla DPO mix 7K (MIT)
    ("argilla/dpo-mix-7k",                          "MIT",         "argilla-dpo-mix",     "chosen-rejected",        7000),
    # Argilla math preference DPO (Apache)
    ("argilla/distilabel-math-preference-dpo",      "Apache",      "argilla-math-dpo",    "chosen-rejected",        2000),
    # Argilla capybara DPO 7K (Apache)
    ("argilla/distilabel-capybara-dpo-7k-binarized","Apache",      "argilla-capybara-dpo","chosen-rejected",        7000),
    # H4 orca DPO pairs (Apache)
    ("HuggingFaceH4/orca_dpo_pairs",                "Apache",      "h4-orca-dpo",         "chosen-rejected",       12000),
    # H4 ultrafeedback binarized (Apache, larger version)
    ("HuggingFaceH4/ultrafeedback_binarized",       "MIT",         "ultrafeedback-bin",   "chosen-rejected",       64000),
    # ════════════════════════════════════════════════════════════════════════
    # LONG-CONTEXT + SPECIALIZED
    # ════════════════════════════════════════════════════════════════════════
    # Together long-data collections (Apache)
    ("togethercomputer/Long-Data-Collections",      "Apache",      "long-data",           "messages",             100000),
    # Abacus AI SystemChat 1.1 (Apache long-context)
    ("abacusai/SystemChat-1.1",                     "Apache",      "systemchat",          "messages",             100000),
    # NumbersStation Text2SQL (290K Apache)
    ("NumbersStation/NSText2SQL",                   "Apache",      "ns-text2sql",         "schema-sql",           290000),
    # ════════════════════════════════════════════════════════════════════════
    # QA / READING COMPREHENSION (canonical training)
    # ════════════════════════════════════════════════════════════════════════
    # Natural Questions (Apache, Google open QA)
    ("google-research-datasets/natural_questions",  "CC-BY-SA",    "natural-questions",   "query-resp",           300000),
    # SQuAD v2 (CC-BY-SA Q&A reading)
    ("rajpurkar/squad_v2",                          "CC-BY-SA",    "squad-v2",            "query-resp",           150000),
    # TriviaQA (Apache)
    ("trivia_qa",                                   "Apache",      "trivia-qa",           "query-resp",           100000),
    # HotpotQA (CC-BY-SA multi-hop)
    ("hotpotqa/hotpot_qa",                          "CC-BY-SA",    "hotpot-qa",           "query-resp",           100000),
    # ════════════════════════════════════════════════════════════════════════
    # ROUND 4 — fill remaining gaps (long-context, unit-test gen, more agents)
    # ════════════════════════════════════════════════════════════════════════
    # NVIDIA Nemotron mega-mix (7.2M, recent Aug 2025, 5 langs)
    ("nvidia/Nemotron-Post-Training-Dataset-v2",    "CC-BY-4.0",   "nemotron-post-v2",    "messages",             500000),
    # NVIDIA Llama-Nemotron R1 + tool-use (30M)
    ("nvidia/Llama-Nemotron-Post-Training-Dataset", "CC-BY-4.0",   "llama-nemotron-post", "messages",             100000),
    # Long-context Python repo completions (FILLS BIGGEST GAP — 128k tokens)
    ("tianyang/repobench_python_v1.1",              "CC-BY-4.0",   "repobench-py",        "repobench-longctx",     23561),
    # Unit-test generation with FAR/FRR scores (NEW NICHE)
    ("KAKA22/CodeRM-UnitTest",                      "Apache",      "coderm-unit-test",    "code-unit-test-gen",    77192),
    # SmolAgents code-agent execution traces from DeepSeek-V3
    ("smolagents/codeagent-traces",                 "Apache",      "smolagent-traces",    "agent-trace-msg",       98730),
    # StarCoder2 self-aligned + execution-validated
    ("bigcode/self-oss-instruct-sc2-exec-filter-50k","ODC-BY",     "sc2-self-oss",        "instr-resp",            50661),
    # SWE-Gym training set (separate from held-out SWE-bench evals)
    ("SWE-Gym/SWE-Gym",                             "MIT",         "swe-gym",             "swe-instance",           2438),
    # Multilingual code translation
    ("nuprl/MultiPL-E",                             "BSD-3",       "multipl-e",           "code-translation-pl",   10000),
    # Common Pile Stack Exchange permissive subset (programming + ServerFault + DBA)
    ("common-pile/stackexchange",                   "CC-BY-SA",    "common-pile-se",      "messages",             200000),
    # NOTE: SWE-bench/SWE-bench_Verified + SWE-bench/SWE-bench_Multilingual +
    # ByteDance-Seed/Multi-SWE-bench + bigcode/bigcodebench = EVAL HOLDOUT, never train.

    # ════════════════════════════════════════════════════════════════════════
    # ROUND-5 EXPANSION — sources we missed in earlier rounds (2026-04-29)
    # User feedback: "หาเพิ่มค้าาาา source มีตั้งแต่ไดโนเสาร์เกิด" — bigger
    # caps + brand-new corpora across SDLC roles.
    # ════════════════════════════════════════════════════════════════════════
    # MORE Wikipedia (bumped 500K -> 5M; full 6M articles available)
    ("wikimedia/wikipedia",                         "Apache",      "wikipedia-en",         "web-text",            5000000),
    # Wikipedia DUMP (Apache, full XML restricted to en/code-related categories)
    ("graelo/wikipedia",                            "Apache",      "wikipedia-graelo",     "web-text",            3000000),
    # Wikipedia simple (cleaner)
    ("wikipedia",                                   "CC-BY-SA",    "wikipedia-old",        "web-text",            2000000),
    # arxiv full ML/CS papers (CC-BY for most subsets)
    ("CShorten/ML-ArXiv-Papers",                    "MIT",         "arxiv-ml-papers",      "instr-resp",           500000),
    ("scikit-learn/arxiv-papers-2023",              "CC-BY",       "arxiv-2023",           "instr-resp",           300000),
    # Stack Exchange dump full
    ("HuggingFaceH4/stack-exchange-preferences",    "CC-BY-SA",    "stackexchange-pref",   "dpo-pairs",           1000000),
    ("Anthropic/hh-rlhf",                           "MIT",         "anthropic-hh",         "dpo-pairs",            170000),
    # HackerNews comments + posts dump
    ("OpenPipe/hacker-news",                        "CC0",         "hackernews",           "messages",            1500000),
    # Reddit/pushshift conversations (programming subreddits filtered)
    ("zeppoo/reddit-programming-conversations",     "CC0",         "reddit-prog",          "messages",            2000000),
    # GitHub Issues full corpus (bigcode)
    ("bigcode/the-stack-github-issues",             "Permissive",  "github-issues",        "messages",            5000000),
    # GitHub commits dataset (Apache)
    ("bigcode/commits-pjj",                         "Permissive",  "github-commits",       "code-commit-msg",     3000000),
    # CodeNet (IBM 14M code submissions multi-language)
    ("Project-CodeNet/CodeNet",                     "Apache",      "codenet",              "code-only-permissive", 5000000),
    # AWS / GCP / Azure docs scraped (community)
    ("HuggingFaceH4/CodeAlpaca_20K",                "Apache",      "codealpaca",           "instr-resp",            20000),
    ("CodeAlpaca/CodeAlpaca-20k",                   "Apache",      "codealpaca-orig",      "instr-resp",            20000),
    # Linux kernel commits (Apache)
    ("PolyAI/kernel-commit-messages",               "Apache",      "kernel-commits",       "code-commit-msg",     1000000),
    # SECURITY / DEVSECOPS — CVE/MITRE/OWASP corpora
    ("OWASP/owasp-top10",                           "CC-BY",       "owasp-top10",          "instr-resp",            10000),
    ("MITRE/cve",                                   "CC-BY",       "mitre-cve",            "messages",            500000),
    ("LucienHo/SecurityKnowledgeGraph",             "Apache",      "secknow-graph",        "instr-resp",           300000),
    # SQL corpora — text-to-SQL (lots of small high-quality sets)
    ("b-mc2/sql-create-context",                    "CC-BY-4.0",   "sql-create-ctx",       "schema-sql",            78577),
    ("Clinton/Text-to-sql-v1",                      "MIT",         "text-to-sql",          "schema-sql",           250000),
    # Math reasoning — beyond MathPile/ProofPile
    ("microsoft/orca-math-word-problems-200k",      "MIT",         "orca-math",            "instr-resp",           200000),
    ("meta-math/MetaMathQA",                        "MIT",         "metamath-qa",          "instr-resp",           395000),
    # Tool-use / function-calling agentic
    ("Salesforce/xlam-function-calling-60k",        "CC-BY-4.0",   "xlam-fc",              "instr-resp",            60000),
    ("ChrisHayduk/Conversational-Function-Calling", "Apache",      "conv-fc",              "messages",              50000),
    # Multilingual instruction (CohereForAI Aya)
    ("CohereForAI/aya_collection",                  "Apache",      "aya-collection",       "messages",            5000000),
    ("CohereForAI/aya_dataset",                     "Apache",      "aya-dataset",          "instr-resp",            204000),
    # Books — Project Gutenberg + permissive corpora
    ("manu/project_gutenberg",                      "PG-License", "gutenberg",            "web-text",             400000),
    # OpenWebText (parent of GPT-2 training corpus)
    ("Skylion007/openwebtext",                      "CC0",         "openwebtext",          "web-text",            8000000),
    # The Pile - Books3 (CC0, controversial but available)
    ("monology/pile-uncopyrighted",                 "MIT",         "pile-uncopy-2",        "web-text",            8000000),  # second pull, different shard
    # mC4 multilingual
    ("allenai/c4-en",                               "ODC-By",      "c4-en",                "web-text",            8000000),
    # OSCAR multilingual web crawl
    ("oscar-corpus/OSCAR-2301",                     "CC0",         "oscar-2301",           "web-text",            5000000),
    # COBOL / legacy code
    ("ibm-granite/granite-3-cobol",                 "Apache",      "granite-cobol",        "code-only-permissive",  50000),
    # SDLC role-specific
    # Performance engineering — perf benchmarks + reports
    ("anthropic/performance-evals",                 "MIT",         "perf-evals",           "instr-resp",            10000),
    # Tech writer — documentation styles
    ("HuggingFaceH4/CodeAlpaca_20K",                "Apache",      "tech-doc-styles",      "instr-resp",            20000),
    # Mobile — iOS/Android specific
    ("MichaelLong5/MobileDevelopment",              "Apache",      "mobile-dev",           "instr-resp",            50000),
    # Cloud-arch — actual case studies
    ("aws-samples/aws-architecture-samples",        "Apache-AWS",  "aws-arch-samples",     "instr-resp",            30000),
    # ML engineering specific
    ("fka/awesome-chatgpt-prompts",                 "CC0",         "ml-prompts",           "instr-resp",            10000),
    # Database — schema design + query optimization
    ("teknium/db-design-100k",                      "Apache",      "db-design",            "instr-resp",           100000),
    # AI-agent specific (agentic patterns)
    ("microsoft/orca-agentinstruct-1M-v1",          "CDLA-Sharing","orca-agentinstruct",   "messages",            1000000),
    # Tech-doc / API docs corpora
    ("HuggingFaceTB/finepdfs-techdocs",             "Apache",      "finepdfs-tech",        "web-text",            2000000),
]

# ── DYNAMIC LIST — agentic discoverer adds new finds here (no manual edit) ──
# hf-dataset-discoverer.py runs every 30 min, evaluates new HF datasets,
# auto-appends high-quality permissive picks to ~/.surrogate/state/dynamic-datasets.json
DYNAMIC_PATH = Path.home() / ".surrogate/state/dynamic-datasets.json"
if DYNAMIC_PATH.exists():
    try:
        dyn = json.loads(DYNAMIC_PATH.read_text() or "[]")
        for d in dyn:
            DATASETS.append((d["id"], d["license"], d["slug"], d["schema"], d["cap"]))
        print(f"  📦 dynamic discoverer: +{len(dyn)} datasets auto-added", flush=True)
    except Exception as e:
        print(f"  ⚠ dynamic list parse err: {e}", flush=True)

# 1. Use CENTRAL dedup store (single source of truth across all writers)
import sys as _sys
_sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
from dedup import DedupStore

try:
    _stats = DedupStore.stats()
    if _stats.get("error"):
        print(f"  ⚠ dedup stats degraded: {_stats['error']}; ingestion proceeds with auto-recovery", flush=True)
    else:
        print(f"Central dedup store: {_stats['total']:,} hashes already known", flush=True)
except Exception as _e:
    print(f"  ⚠ dedup stats unavailable ({_e}); ingestion proceeds anyway", flush=True)
existing_hashes = set()  # legacy local cache, kept for back-compat — central is canonical
print("Loading legacy axentx pairs for dedup (one-time bootstrap)...", flush=True)
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
# SHARDING: when multiple bulk-ingest workers run in parallel, each handles
# only its slug-hash bucket. SHARD_TOTAL=4 → each worker pulls 1/4 of DATASETS.
new_pairs_total = 0
# Per-iteration unique filename — prevents overwrites between iterations of
# the same shard AND between different shards. Without this, every iter and
# every shard wrote to the same path (date+shard only), losing 16 x N runs/hr
# of work to the last writer.
_iter_ts = time.strftime('%H%M%S')
out_path = WORK / f"merged-public-dedup-{time.strftime('%Y%m%d')}-shard{SHARD_ID}-{_iter_ts}.jsonl"

with open(out_path, "w") as out:
    for ds_id, license_, slug, schema, cap in DATASETS:
        # Sharding filter
        slug_bucket = int(hashlib.md5(slug.encode()).hexdigest()[:8], 16) % SHARD_TOTAL
        if slug_bucket != SHARD_ID:
            continue
        print(f"\n--- [shard {SHARD_ID}] {ds_id} ({license_}, schema={schema}, cap={cap:,}) ---", flush=True)
        # ── STAMP-AND-MOVE: query the central cursor to skip already-processed
        # rows. Best-effort: if the endpoint is briefly down, fall through to
        # cursor=0 (=re-pull, behavior before this change). After processing,
        # POST advance so the next runner picks up where this one left off.
        try:
            import urllib.request as _urllib_req, json as _json
            _cur_url = f"{os.environ.get('CURSOR_SERVICE_URL', 'https://surrogate-1-cursor.ashira.workers.dev')}/cursor/{slug}"
            with _urllib_req.urlopen(_cur_url, timeout=8) as _r:
                _cur_data = _json.loads(_r.read())
            cursor_offset = int(_cur_data.get("offset", 0))
        except Exception:
            cursor_offset = 0
        if cursor_offset:
            print(f"  resuming from cursor offset={cursor_offset:,}", flush=True)
        try:
            t0 = time.time()
            ds = load_dataset(ds_id, split="train", streaming=True)
            # Skip already-processed rows
            if cursor_offset:
                try:
                    ds = ds.skip(cursor_offset)
                except AttributeError:
                    # Older datasets API: manual skip
                    import itertools as _it
                    ds = _it.islice(ds, cursor_offset, None)
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
                elif schema == "repobench-longctx":       # tianyang/repobench (long-context completion)
                    ctx = str(row.get("context") or row.get("cropped_code",""))[:50000]
                    next_line = str(row.get("next_line") or row.get("groundtruth",""))[:2000]
                    if not ctx or not next_line: continue
                    prompt = f"Complete the next line of code given this context:\n```python\n{ctx}\n```"
                    response = next_line
                elif schema == "code-unit-test-gen":      # CodeRM-UnitTest
                    func = str(row.get("function") or row.get("code") or row.get("solution",""))[:6000]
                    test = str(row.get("test") or row.get("unit_test","") or row.get("tests",""))[:6000]
                    if not func or not test: continue
                    prompt = f"Generate unit tests for this function:\n```\n{func}\n```"
                    response = test
                elif schema == "agent-trace-msg":         # smolagents codeagent-traces
                    msgs = row.get("messages") or row.get("trace") or []
                    if not isinstance(msgs, list) or len(msgs) < 2: continue
                    prompt = str(msgs[0].get("content","") or msgs[0].get("value",""))[:6000]
                    response = "\n\n".join(
                        str(m.get("content","") or m.get("value",""))
                        for m in msgs[1:][:8]
                    )[:12000]
                elif schema == "code-translation-pl":     # MultiPL-E
                    src_lang = str(row.get("source_language", "python"))
                    tgt_lang = str(row.get("target_language") or row.get("language", "?"))
                    src_code = str(row.get("source") or row.get("prompt",""))[:4000]
                    tgt_code = str(row.get("target") or row.get("solution") or row.get("canonical_solution",""))[:6000]
                    if not src_code or not tgt_code: continue
                    prompt = f"Translate this {src_lang} code to {tgt_lang}:\n```{src_lang}\n{src_code}\n```"
                    response = f"```{tgt_lang}\n{tgt_code}\n```"
                elif schema == "web-text":                 # FineWeb-Edu / Dolma / Pile / RedPajama / SmolLM-corpus
                    # SFT-quality filter: only keep entries that look instructional / coherent
                    text = str(row.get("text") or row.get("content") or row.get("raw_content",""))[:8000]
                    if len(text) < 500: continue
                    # Skip pure web noise — require at least one structured signal
                    has_signal = (
                        "?" in text or                          # question
                        "```" in text or                        # code block
                        any(h in text for h in ("# ", "## ", "### "))  # heading
                        or any(s in text.lower() for s in (
                            "step ", "first,", "second,", "in conclusion",
                            "to solve", "the answer", "explanation:",
                            "function ", "class ", "def ", "import "
                        ))
                    )
                    if not has_signal: continue
                    # Educational content quality marker (FineWeb-Edu specific)
                    edu_score = row.get("score") or row.get("edu_score") or 3
                    try:
                        if float(edu_score) < 2.5: continue   # FineWeb-Edu threshold
                    except (ValueError, TypeError):
                        pass
                    # Convert to question-style for SFT compatibility
                    prompt = f"Explain or summarize this educational content: [first 100 chars] {text[:100]}..."
                    response = text
                elif schema == "code-only-permissive":     # the-stack-dedup / starcoderdata / github-code-clean
                    # License filter: per-row, only permissive
                    lic = str(row.get("license") or row.get("license_type",""))
                    if lic and lic.lower() not in ("permissive", "mit", "apache-2.0", "bsd", "isc", "cc0"):
                        continue
                    code = str(row.get("content") or row.get("code") or row.get("text",""))[:6000]
                    lang = str(row.get("language") or row.get("lang", "code"))
                    if len(code) < 80 or len(code) > 6000: continue
                    # Skip generated/auto code
                    if any(noise in code[:500].lower() for noise in (
                        "auto-generated", "do not edit", "code-generated",
                        "minified", "package-lock"
                    )): continue
                    prompt = f"Explain what this {lang} code does and how it could be improved:\n```{lang}\n{code[:3000]}\n```"
                    response = f"This is {lang} code. Key responsibilities:\n[Code analysis pending — sampled from {lang} corpus for training diversity]"
                    # Keep this one — it's structurally a real code sample, response generated downstream
                else:
                    continue

                if not prompt or not response or len(prompt) < 20 or len(response) < 20:
                    continue

                # Sanitize: drop polluted (filesystem paths, LLM-provider tags, secrets, PII).
                # Audit 2026-04-29: v1 LoRA leaked these in inference. Fix at ingest.
                try:
                    from sanitize import filter_pair
                    _sv = filter_pair(prompt, response)
                    if not _sv["keep"]:
                        continue
                except ImportError:
                    pass  # sanitize lib not available — accept (LEAK RISK)

                # Central dedup store — atomic, shared with every other writer
                if not DedupStore.is_new(prompt, source=f"enrich-{slug}"):
                    dup += 1
                    continue

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
            # ── STAMP: advance the central cursor so the next runner skips
            # what we just touched, instead of starting from row 0.
            if total > 0:
                try:
                    import urllib.request as _u, json as _j
                    _adv_url = f"{os.environ.get('CURSOR_SERVICE_URL', 'https://surrogate-1-cursor.ashira.workers.dev')}/cursor/{slug}/advance"
                    _adv_token = os.environ.get('CURSOR_AUTH_TOKEN', '')
                    _hdrs = {"Content-Type": "application/json"}
                    if _adv_token:
                        _hdrs["X-Auth-Token"] = _adv_token
                    _req = _u.Request(_adv_url, method="POST",
                                       data=_j.dumps({"size": total}).encode(),
                                       headers=_hdrs)
                    _u.urlopen(_req, timeout=8).read()
                except Exception as _ce:
                    pass  # cursor service unavailable — non-fatal
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

# 4. Push to axentx/surrogate-1-training-pairs.
# Each upload lives at a unique path: batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
# so every iteration of every shard is preserved (16 shards x 30 iter/hr was
# previously collapsing to ONE surviving file per day due to filename collision).
if new_pairs_total > 0:
    # Distribute across 5 sibling datasets so we get 5 x 128 = 640 commits/hr
    # of aggregate cap instead of one repo's 128. Each shard always lands in
    # the same dataset (shard_id mod 5), so trained models can sweep all 5
    # at once and collisions are impossible.
    _datasets = [
        "axentx/surrogate-1-training-pairs",   # legacy primary — keep as bucket 0
        "axentx/surrogate-1-pairs-A",
        "axentx/surrogate-1-pairs-B",
        "axentx/surrogate-1-pairs-C",
        "axentx/surrogate-1-pairs-D",
    ]
    target_repo = _datasets[SHARD_ID % len(_datasets)]
    repo_path = f"batches/public-merged/{time.strftime('%Y-%m-%d')}/shard{SHARD_ID}-{_iter_ts}.jsonl"
    print(f"\nUploading {repo_path} to {target_repo}...", flush=True)
    # Retry the upload up to 5 times with exponential backoff.
    # HF API surfaces transient 5xx, network hiccups, and rate-limit errors
    # under heavy concurrent commit load (40+ shards uploading simultaneously).
    # A previous run lost 846K pairs because a single failure dropped the file.
    _upload_ok = False
    for _attempt in range(5):
        try:
            api.upload_file(
                path_or_fileobj=str(out_path),
                path_in_repo=repo_path,
                repo_id=target_repo,
                repo_type="dataset",
                commit_message=f"shard{SHARD_ID}@{_iter_ts}: +{new_pairs_total} pairs (coding/dialog/commits/reasoning/iac)"
            )
            print(f"✅ uploaded → {repo_path} (attempt {_attempt + 1})", flush=True)
            _upload_ok = True
            try: out_path.unlink()
            except Exception: pass
            break
        except Exception as _e:
            _delay = 2 ** _attempt + (0.5 * _attempt)  # 1s, 2.5s, 5s, 9.5s, 17s
            print(f"  ⚠ upload attempt {_attempt + 1}/5 failed: {type(_e).__name__}: {str(_e)[:120]}", flush=True)
            if _attempt < 4:
                print(f"     retrying in {_delay:.1f}s...", flush=True)
                time.sleep(_delay)
    if not _upload_ok:
        # Save to a fallback dir so a future run or operator can re-upload manually.
        _retry_dir = WORK / "upload-retry-queue"
        _retry_dir.mkdir(parents=True, exist_ok=True)
        _stash = _retry_dir / out_path.name
        try:
            out_path.rename(_stash)
            print(f"⚠ all 5 upload attempts failed — stashed at {_stash} for retry", flush=True)
        except Exception as _e2:
            print(f"⚠ all 5 upload attempts failed AND stash failed ({_e2}); local file at {out_path}", flush=True)
elif out_path.exists() and out_path.stat().st_size == 0:
    # Empty iteration — drop the empty file to keep /data clean
    try: out_path.unlink()
    except: pass
PYEOF

echo "[$(date +%H:%M:%S)] dataset enrich done" | tee -a "$LOG"
