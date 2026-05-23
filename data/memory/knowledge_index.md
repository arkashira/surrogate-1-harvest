File unchanged since last read. The content from the earlier read_file result in this conversation is still current — refer to that instead of re-reading.

- Pattern: business research with knowledge-rag pipeline | Fix: After running a market analysis script (e.g., granite-business-research.sh), execute knowledge-rag to query top hub and related docs for contextual insights; Tags: #business-research #knowledge-rag #graph
- Pattern: top-hub doc insight (2026-04-27) | Fix: Review the most-connected hub (e.g., "MOC") before planning tasks; Tags: #knowledge-rag #graph #hub
- Pattern: opus pr reviewer script exec error | Fix: Ensure wrapper script has proper Bash shebang, is executable, invoke via Bash, set SHELL=/bin/bash in crontab | Tags: #bash #script-error #opus-pr-reviewer #cron
- Pattern: active-learning wrapper exec error | Fix: Ensure wrapper script has Bash shebang (#!/usr/bin/env bash), is executable (chmod +x), invoke via Bash (bash <script> "$@"), set SHELL=/bin/bash in crontab | Tags: #script #cron #bash #active-learning #wrapper

## Surrogate-1 Training Pipeline (2026-04-29)
- Pattern: pyarrow CastError on HF dataset with mixed schema files | Fix: Don't use `load_dataset(streaming=True)` for repos with heterogeneous file schemas; download each file individually via `hf_hub_download` then project to {prompt, response} only at parse time | Tags: #training #pyarrow #hf-datasets #schema #surrogate-1
- Pattern: HF API rate limit 429 (1000 req/5min) | Fix: Avoid `list_repo_files` recursive on big repos (paginates 100x). Use `list_repo_tree(path, recursive=False)` per folder. CDN downloads (`/resolve/main/`) are NOT counted. After 429: wait 360s before retry | Tags: #huggingface #rate-limit #api #ingestion
- Pattern: HF commit cap 128/hr/repo blocks ingestion | Fix: Spread writes across N sibling repos (5 siblings = 640/hr aggregate). Hash slug → pick repo deterministically | Tags: #huggingface #commit-cap #throughput
- Pattern: Lightning H200 not in default cloud account | Fix: Lightning has 9 cloud accounts; H200 only in `lightning-lambda-prod` (paid). Free tier falls to `lightning-public-prod` → L40S max. Sweep clouds × sizes in priority order | Tags: #lightning-ai #gpu #h200 #free-tier
- Pattern: existing Lightning Studio reuse | Fix: Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse Running ones — saves 80hr/mo quota | Tags: #lightning-ai #quota
- Pattern: dataset-mirror writes mixed-schema files to enriched/ | Fix: Project to {prompt, response} only before upload. Move attribution to filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`). Don't add `source` / `ts` cols | Tags: #ingestion #schema #surrogate-1
- Pattern: Mac=CLI rule + heavy compute on remote | Fix: Mac runs ONLY orchestration scripts (Lightning SDK launcher, HF API). Training → Lightning Studio. Ingestion → HF Space. LLM burst → Cerebras/Groq/etc. Never run model.from_pretrained() on Mac | Tags: #architecture #compute #surrogate-1

## HF CDN Bypass (THE KEY INSIGHT 2026-04-29)
- Pattern: HF API rate-limit blocks dataset training | Fix: Public dataset files at `https://huggingface.co/datasets/{repo}/resolve/main/{path}` can be downloaded with NO Authorization header — bypasses /api/ auth-check rate limit entirely. CDN tier has separate (much higher) limits | Tags: #huggingface #cdn #rate-limit-bypass #training
- Pattern: pre-list file paths once, embed in training script | Fix: Single API call from Mac (after rate-limit window clears) to `list_repo_tree(path, recursive=False)` for one date folder. Save list to JSON. Embed in train.py. Lightning training does CDN-only fetches with zero API calls during data load | Tags: #training #api-strategy #file-list
- Pattern: studio reuse instead of recreate | Fix: `for s in Teamspace.studios: if s.name == X and s.status == 'Running': use s` — saves Lightning 80hr/mo quota when iterating training scripts | Tags: #lightning-ai #quota
- Pattern: Lightning idle stop kills training | Fix: When studio stops, training process dies. Check status before each `.run()` call; restart with `target.start(machine=Machine.L40S)` if stopped | Tags: #lightning-ai #idle-timeout

## Kaggle KGAT Token Auth (2026-04-29)
- Pattern: Kaggle KGAT_* token with kaggle CLI 2.1.0 returns 401 | Fix: KGAT format uses Bearer auth (not Basic). Bypass CLI, use raw HTTP: `requests.post("https://www.kaggle.com/api/v1/kernels/push", headers={"Authorization": f"Bearer {token}"}, json=body)` | Tags: #kaggle #auth #api
- Pattern: kernels/push 400 "Could not convert to integer" on `id` field | Fix: New API uses `slug`, `newTitle`, `text` (not `id`, `title`, `code`). camelCase keys. slug must be `username/kernelname` | Tags: #kaggle #api-schema
- Pattern: kernels/push 403 "Phone verification required" | Fix: Set `isPrivate: True` to skip phone verification for new accounts | Tags: #kaggle #verification
- Pattern: HF ZeroGPU requires PRO/Team subscription | Fix: Free Spaces only get cpu-basic. ZeroGPU = $9/mo PRO or org Team plan. For free GPU: use Colab (T4) or Kaggle (T4×2 30hr/wk) | Tags: #huggingface #zerogpu #free-tier-limits
