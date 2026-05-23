# Surrogate-1 Feature Roadmap

**Updated**: 2026-04-28
**Status legend**: ✅ shipped │ 🚧 in progress │ ⏳ planned │ 💡 idea

---

## 🟢 Already Shipped (Foundation)

### Pipeline (parallel orchestrate)
- ✅ 6-stage chain: SA → [Architect ∥ QA-TDD] → DEV → [QA-Verify ∥ OPS] → Reviewer
- ✅ Direct LLM call (skip broken tool-loop)
- ✅ Marker-extraction → real code blocks → real files in cwd
- ✅ Auto-commit + git push on APPROVE
- ✅ 12-rung LLM ladder (Cerebras / Groq / Gemini × 2 / Samba / GH Models / Chutes / OR × 2 / **HF Router × 4**)

### Data + Knowledge
- ✅ 26 public datasets covering all SDLC domains
- ✅ Training-pair feedback loop (every stage → ~/.surrogate/training-pairs.jsonl → HF dataset every 3 min)
- ✅ Web research preamble (DDG search → context for PRD/orchestrate)
- ✅ Agentic crawler (URL frontier + visited stamps + BFS link discovery, 6 workers)
- ✅ Skill synthesis daemon (3-min cycles → ~/.surrogate/skills/{cat}/SKILL.md)
- ✅ Continuous scrape (8 workers, 5-30s cool-down)

### Models (Ollama on HF)
- ✅ qwen3-coder:30b-a3b (primary, 16GB MoE)
- ✅ devstral:24b (Mistral SWE-agent, 53.6% SWE-bench)
- ✅ qwen2.5-coder:14b (fallback)
- ✅ yi-coder:9b (128k context)
- ✅ nomic-embed-text (RAG embeddings)

### Agent Roster (19 SDLC experts)
- ✅ solution-architect, tech-architect (design)
- ✅ dev-frontend, dev-backend, dev-mobile, dev-fullstack, dev-database (impl)
- ✅ qa-engineer, qa-perf, qa-security (test)
- ✅ devops, sre, cloud-architect (infra)
- ✅ devsecops, cloud-security (security)
- ✅ data-engineer, ml-engineer (data/ML)
- ✅ tech-writer, reviewer (docs/gate)

### Infrastructure
- ✅ HF Space (CPU 16GB free) running 24/7
- ✅ /data persistent volume (state + logs + memory + skills + sessions + training-pairs)
- ✅ Backward-compat symlinks (~/.claude/* → ~/.surrogate/*)
- ✅ Mac CLI clean (20 essential files only, 118 daemons archived)
- ✅ Status server: /, /health, /logs/{name}, /logs-list

---

## 🔴 Must-Have (next 30 days)

### Reliability + Observability
1. ⏳ Heartbeat alarm → Discord webhook if HF Space down >5 min
2. ⏳ Auto-retry on transient errors (provider 429/503 → wait + retry next rung)
3. ⏳ Cost meter per stage (tokens × $/1M, alert >$1/day)
4. ⏳ Regression test suite (run nightly: orchestrate test fixtures, expect APPROVE)
5. ⏳ Dataset upload deduplication (md5 of slice → skip if same as last)
6. ⏳ Token-pool health check (rotate to next when 429)
7. ⏳ Disk usage alert (>80% /data → cleanup oldest scrape state)
8. ⏳ Memory leak watchdog (kill daemon RSS >1.5GB, restart)
9. ⏳ Crash recovery (auto-resume cron loop on SIGCHLD)
10. ⏳ Snapshot scrape ledger to HF dataset weekly

### PRD + Project bootstrap
11. ⏳ Claude Projects-style PRD wizard (single description input → auto-extract → 1-3 follow-ups → PRD)
12. ⏳ PRD template library (web app / API / CLI / mobile / data pipeline / ML)
13. ⏳ Auto-detect existing repo → reverse-engineer surrogate.md
14. ⏳ PRD versioning (v1, v2 with diff)
15. ⏳ "Spec mode" — refine PRD interactively before any code

### Pipeline quality
16. ⏳ Self-critique loop (after dev: model A reviews model B output → re-dev if NEEDS-WORK)
17. ⏳ Regression test on touched files (re-run existing tests)
18. ⏳ Lint + type-check + security scan in pipeline (ruff, mypy, semgrep)
19. ⏳ Diff approval UI (show changes before commit, esp. yolo mode)
20. ⏳ Search-replace block edits (Aider-style, less risky than full rewrite)

### Domain expert routing
21. ⏳ Auto-route DEV stage to specialist (frontend/backend/mobile/iac) based on task keywords
22. ⏳ Multi-specialist parallel work (e.g., backend API + frontend UI in same task → spawn both)
23. ⏳ Specialist-specific eval (frontend agent → check WCAG; backend → check N+1)

### Memory + Context
24. ⏳ Episodic memory (last 50 sessions retrieval for similar tasks)
25. ⏳ Procedural memory (how-to library auto-generated from successful runs)
26. ⏳ Project context cache (surrogate.md + repo-map persisted across sessions)
27. ⏳ Cross-project pattern share (skill from project A → applicable to project B)
28. ⏳ Long-term retention (key decisions → ADR auto-generation)

### Self-improvement loop
29. ⏳ Reflexion lessons → injected into next-similar-task prompt
30. ⏳ Failed orchestrate → root-cause analysis → improvement queue
31. ⏳ Weekly LoRA fine-tune trigger (on accumulated training pairs, autotrain)
32. ⏳ A/B test prompts (variant A vs B, pick winner by APPROVE rate)
33. ⏳ Voyager-style skill crystallization (pattern repeated 3+ times → permanent skill)

### Datasets + Training
34. ⏳ SRE postmortem corpus (scrape danluu/post-mortems → ~600 incident → instruction-pair)
35. ⏳ AWS Well-Architected synthetic Q/A (PDFs → distilabel pipeline → 5k pairs)
36. ⏳ Internal axentx code → instruction pairs (commit messages + diffs)
37. ⏳ Training pair quality scoring (filter low-quality before HF upload)
38. ⏳ DPO preference pairs from reviewer (chosen/rejected from REWORK cycles)
39. ⏳ Synthetic ADR generation (real OSS examples → expand via distilabel)

### Tools + Integrations
40. ⏳ MCP client support (Claude Desktop schema — connect external tools)
41. ⏳ ToolSearch lazy-load (don't blow context on full tool list)
42. ⏳ Constitutional Critic from ~/.surrogate/agents/roster.json (auto-load)
43. ⏳ Repo-map context (tree-sitter symbol graph → smarter file selection)
44. ⏳ Tool-call traces saved as training data (every tool use → pair)

### Security + Safety
45. ⏳ Secret-scan pre-commit hook (gitleaks integration)
46. ⏳ Rate limit per-IP (HF Space /chat endpoint)
47. ⏳ Allowlist/denylist for git push (don't push to main without flag)
48. ⏳ PII scrubber for training pairs (remove emails, IPs, names before upload)
49. ⏳ Sandbox tool execution (no rm -rf, no curl |sh, no destructive ops)
50. ⏳ Audit log for every orchestrate run (who/what/when/result)

### Multi-modal + I/O
51. ⏳ Voice input (Whisper transcribe → surrogate)
52. ⏳ Image input (architectural diagrams → analysis)
53. ⏳ Screen recording → video → tutorial agent
54. ⏳ Discord voice channel (TTS responses)

### CLI UX
55. ⏳ /resume <session-id> (continue past session)
56. ⏳ /diff (show pending changes before commit)
57. ⏳ /undo (rollback last orchestrate via git stash)
58. ⏳ /share (publish session as gist for review)
59. ⏳ Tab autocomplete for slash commands
60. ⏳ Cost-meter live in statusline (running $ this session)

### Cloud / multi-region
61. ⏳ Mirror to Cloudflare Workers AI (free tier backup)
62. ⏳ Egress whitelist for Discord on HF Pro tier
63. ⏳ HF Space upgrade auto-scale (when load > 80%)
64. ⏳ Backup strategy: weekly snapshot of /data → HF dataset

### Codebase intelligence
65. ⏳ Symbol search (tree-sitter index, not just text grep)
66. ⏳ Cross-file refactor (rename across project safely)
67. ⏳ Type-aware code completion (LSP integration)
68. ⏳ Dead code detection (vulture, ts-prune)
69. ⏳ Dependency graph viz (per-project)

### Training data flywheel
70. ⏳ Trace storage on HF (axentx/surrogate-1-traces dataset)
71. ⏳ Auto-tag training pairs by domain (frontend/backend/etc)
72. ⏳ Quality gate before training pair upload (≥ N tokens, well-formed)
73. ⏳ Weekly eval on SWE-bench-Lite (track improvement)
74. ⏳ DPO data generation (REWORK cycles → preference pairs)

### Discord + notifications
75. ⏳ Discord webhook for every commit (axentx repo notifications)
76. ⏳ Daily digest webhook (commits + pairs + scrape stats)
77. ⏳ Failure alerts (orchestrate fail → ping)
78. ⏳ Slash commands `/orchestrate "task"` from Discord

### HF integrations
79. ⏳ TEI server (text-embeddings-inference) for RAG
80. ⏳ TGI server (text-generation-inference) for self-hosted LLM
81. ⏳ autotrain weekly LoRA on training pairs
82. ⏳ HF Inference Providers as primary (paid bypass)
83. ⏳ HF Spaces gradio UI (visualize chain status)

### Agent quality
84. ⏳ Specialist eval per agent (e.g., dev-backend on RealWorld benchmark)
85. ⏳ Multi-model consensus on critical decisions (architecture, security)
86. ⏳ Constitutional rules (no hard-coded secrets, validate input)
87. ⏳ Tool use tracking per agent (which tools each agent calls)
88. ⏳ Persona consistency check (review for tone/style mid-thread)

### Project management
89. ⏳ Burndown chart per surrogate.md plan
90. ⏳ Story-point estimation from PRD
91. ⏳ Auto-create GitHub issues from `- [ ]` plan items
92. ⏳ PR description auto-write from commit list
93. ⏳ Sprint retrospective auto-summary

### Performance
94. ⏳ Profile + optimize orchestrate cycle time (target < 90s p50)
95. ⏳ Streaming responses (LLM tokens flow live, don't wait for full)
96. ⏳ Local cache for repeated identical prompts
97. ⏳ Parallel model calls (race fastest-first, kill rest)
98. ⏳ Edge inference (qwen3-coder on Cerebras WaferScale via API)

### Compliance + Governance
99. ⏳ License audit per file generated (OSS license compatibility)
100. ⏳ Commit signing (gpg/sigstore)

---

## 💡 Nice-to-Have (future)

### Multi-agent collaboration
1. 💡 MoA (Mixture of Agents) — 3 LLMs propose, judge picks best
2. 💡 Debate mode (2 agents argue, third synthesizes)
3. 💡 Tournament-style code review (3 reviewers, majority verdict)
4. 💡 Hierarchical agents (manager → workers → reporter)
5. 💡 Autonomous research squad (3 agents split topics, merge findings)

### UI / UX
6. 💡 Web dashboard (real-time pipeline status, training pair count, model health)
7. 💡 VSCode extension (`surrogate /auto` from editor)
8. 💡 IntelliJ plugin
9. 💡 Mobile app (iOS/Android) for on-the-go orchestrate
10. 💡 Apple Watch glance (current task status)

### Voice + Audio
11. 💡 Whisper realtime transcription
12. 💡 ElevenLabs TTS for status reports
13. 💡 Daily audio briefing podcast
14. 💡 Voice clone of user for replies

### Visual
15. 💡 Architecture diagram auto-generation (mermaid → SVG)
16. 💡 Dependency graph live render
17. 💡 Heat map of code changes per file
18. 💡 3D codebase visualization (gource-style)

### Integrations
19. 💡 Linear / Jira sync (pull tickets, update status)
20. 💡 Slack bot
21. 💡 Microsoft Teams bot
22. 💡 Notion sync (PRD ↔ Notion page)
23. 💡 Figma plugin (design → code via DEV agent)
24. 💡 Storybook integration (component dev)
25. 💡 Sentry integration (errors → fix queue)
26. 💡 PagerDuty integration (incident → SRE agent)
27. 💡 GitHub Copilot bridge (delegate to Surrogate for complex)
28. 💡 Cursor IDE integration

### ML / Self-improvement
29. 💡 RLHF from APPROVE/REWORK signals
30. 💡 RLAIF (AI feedback on agent outputs)
31. 💡 Continual pre-training on axentx code corpus
32. 💡 Distillation (qwen-coder-30B → 7B for edge)
33. 💡 Quantization-aware fine-tuning
34. 💡 Speculative decoding for faster inference
35. 💡 Mixture-of-experts custom training

### Datasets
36. 💡 Real-time scrape of GitHub trending (every 1h)
37. 💡 Scrape Hacker News top stories daily
38. 💡 Scrape Reddit r/programming weekly
39. 💡 Scrape Twitter dev threads (X API tier 1 = $100/m, skip)
40. 💡 Curated YouTube transcripts (developer talks, RustConf, KubeCon)
41. 💡 Scrape arxiv-sanity for AI papers
42. 💡 Crawl AWS/GCP/Azure docs nightly
43. 💡 PR diff archive (axentx own PRs as training)
44. 💡 Stack Overflow accepted answers (dump filter)
45. 💡 GitHub issue resolutions (closed issue → PR linkage)

### Cloud / Deployment
46. 💡 Multi-region HF Spaces (ap-southeast + us-east + eu-west)
47. 💡 K8s deployment manifests (move beyond HF when scale demands)
48. 💡 Kubernetes operator for axentx orchestration
49. 💡 Lambda@Edge for global low-latency inference
50. 💡 IPFS publish of PRDs (decentralized)

### Privacy + Security
51. 💡 E2E encryption for Discord chat
52. 💡 Air-gapped mode (Mac-only, no cloud)
53. 💡 Federated learning (multiple users contribute, no central data)
54. 💡 Zero-knowledge proofs for code provenance
55. 💡 Confidential computing (Intel SGX) for sensitive code
56. 💡 GDPR compliance toolkit (PII scrub, right-to-delete)
57. 💡 SOC 2 Type II readiness checklist
58. 💡 ISO 27001 audit prep

### Specialty agents
59. 💡 Compiler engineer (LLVM, optimization passes)
60. 💡 Embedded systems (microcontroller code, real-time)
61. 💡 Game dev (Unity, Unreal, Godot)
62. 💡 Blockchain (Solidity, smart contracts, security)
63. 💡 Quantum computing (Qiskit, circuits)
64. 💡 Robotics (ROS, motion planning)
65. 💡 Bioinformatics (BLAST, sequence analysis)
66. 💡 Quantitative finance (backtesting, risk)
67. 💡 Climate modeling
68. 💡 Legal tech (contract review)

### Education
69. 💡 Teach mode (explain decisions step-by-step for learners)
70. 💡 Pair programming mode (turn-taking with user)
71. 💡 Code review school (annotated learning examples)
72. 💡 Daily challenge generator (LeetCode-style, personalized)
73. 💡 Concept explainer (DDD, hexagonal, CAP theorem on demand)

### Productivity
74. 💡 Calendar integration (block focus time when in flow)
75. 💡 Pomodoro mode
76. 💡 Energy/mood tracker (suggest break when fatigued)
77. 💡 Distraction blocker (no Twitter when Surrogate active)
78. 💡 Focus music generator (lo-fi via Suno API)

### Emerging tech
79. 💡 ASI safety guardrails (per Anthropic Constitutional AI)
80. 💡 World model simulation (test ideas in synth environment)
81. 💡 Causal reasoning (vs correlation)
82. 💡 Theorem prover integration (Lean, Coq for verified code)
83. 💡 Differential privacy in training
84. 💡 Explainable AI for code reviews

### Localization
85. 💡 Thai-native pipeline (โค้ดและ comments เป็นไทย)
86. 💡 Japanese, Korean, Chinese support
87. 💡 RTL languages (Arabic, Hebrew)
88. 💡 Local LLM Thai-fluent (typhoon, openthaigpt)
89. 💡 Cultural code review (idioms per locale)

### Marketing + community
90. 💡 Public Surrogate-1 demo Space (read-only)
91. 💡 Twitter bot posts daily Surrogate-1 wins
92. 💡 GitHub discussions for community
93. 💡 Discord server for users
94. 💡 Newsletter (weekly improvements)
95. 💡 Blog (axentx engineering)

### Speculative
96. 💡 Surrogate-2 (full local inference, no cloud dep)
97. 💡 Custom silicon (qwen-coder optimized FPGA)
98. 💡 BCI integration (Neuralink-style direct intent)
99. 💡 Physical robot (Boston Dynamics + Surrogate brain)
100. 💡 ASI alignment research collaboration

---

## Current Cadence (auto-running on HF)

| Task | Frequency | Status |
|---|---|---|
| Continuous scrape | 8 workers, 5-30s cool-down | ✅ |
| Agentic crawler | 6 workers, BFS frontier | ✅ |
| Skill synthesis | every 3 min | ✅ |
| surrogate-dev-loop | every 2 min | ✅ |
| work-queue producer | every 5 min | ✅ |
| training-pair push to HF | every 3 min | ✅ |
| auto-orchestrate-loop | every 20 min | ✅ |
| research-apply | every 30 min | ✅ |
| keyword tuner | every 60 min | ✅ |
| research-loop | every 6h | ✅ |
| dataset-enrich | every 12h | ✅ |

## Verified working (2026-04-28)
- 5 commits to HF dataset in 12 min (~4047 pairs uploaded)
- Pipeline produces real Python/Go code with DDD patterns
- Reviewer issues APPROVE / REWORK / REJECT verdicts
- Training feedback loop closing (every stage → HF)
