---
title: V14+ Multimodal & Computer-Use Capability — Training-Side Techniques
date: 2026-05-01
tags: [surrogate-1, v14, multimodal, computer-use, vlm, gui-agent, trends-2026]
status: active
related:
  - "[[v13-frontier-capability]]"
  - "[[v13-multi-agent-baked-in]]"
  - "[[training-tooling-2026-Q2]]"
  - "[[opensource-releases-2026-Q2]]"
  - "[[autonomous-24x7]]"
---

# V14+ Multimodal & Computer-Use Capability — Training-Side Techniques

> **Goal**: Bake "Surrogate sees and clicks" capability INTO weights — not just bolt-on tools. Cover frontier (Anthropic CUA, OpenAI Operator/CUA, Gemini 2.5/Astra, Qwen3-VL, OpenCUA, UI-TARS, Fara-7B) + datasets + training recipes that fit the V14+ constraints (Qwen2.5-Coder-7B base, T4×2 16GB×2 free Kaggle, no $$).

> **Bottom line up front**: A 7B-class model achieving 30%+ OSWorld is now reproducible (UI-TARS-1.5-7B, Fara-7B, OpenCUA-7B, ARPO 29.9%). The cheapest path for Surrogate is **NOT pixel-vision** — it's **DOM-as-text + accessibility-tree-as-text** (text-only continuation of Qwen2.5-Coder-7B), with optional ColQwen2-style page embedding for read-only doc retrieval. Vision tower is feasible only by switching base to Qwen2.5-VL-7B/Qwen3-VL-8B at 24GB+ VRAM cost.

---

## 1. Frontier Closed Models — Computer Use SOTA (2024-2026)

### 1.1 Anthropic Claude — Computer Use (Oct 2024 → 2026)
- **Released**: Oct 2024 with Claude 3.5 Sonnet (computer-use beta).
- **OSWorld trajectory**:
  - Claude 3.5 Sonnet (Oct 2024): ~14% (initial)
  - Claude 3.7 Sonnet: rose into 30s-40s
  - Claude Opus 4.5 (Nov 2025): **66.26%** OSWorld
  - Claude Opus 4.7: **78.0%** OSWorld-Verified (ahead of GPT-5.4 at 75.0%)
  - Claude Sonnet (Feb 2026): **72.5%**
- **Training method (publicly disclosed bits)**:
  - Vision-trained on screenshots; emits structured tool calls (`computer_use` tool with `screenshot/click/type/key`)
  - RL on agentic trajectories (similar to o1/o3-style)
  - **Vercept acquisition (2025)** to optimize CUA-specific training
- **Key takeaway for Surrogate**: ANY 7B-class match impossible without RL+millions of trajectories. But the *tool-call interface* (action schema) is replicable.
- URL: https://www.anthropic.com/news/3-5-models-and-computer-use ; https://www.techzine.eu/news/analytics/139119/

### 1.2 OpenAI Operator / CUA Model (Jan 2025)
- **Released**: Jan 23 2025 (Operator product); July 17 2025 folded into ChatGPT "agent mode".
- **Architecture**: GPT-4o vision backbone + reasoning-style RL post-training (similar to o1/o3).
- **Benchmarks (CUA)**:
  - **OSWorld: 38.1%** (full computer use)
  - **WebArena: 58.1%**
  - **WebVoyager: 87%**
- **Tool interface**: screenshot in → action out (mouse/keyboard).
- **Training disclosed**: "RL with techniques similar to o1/o3" + multimodal alignment.
- **Public proxy**: openai-cua-sample-app on GitHub (action schema + scaffolding).
- URL: https://openai.com/index/computer-using-agent/ ; https://github.com/openai/openai-cua-sample-app

### 1.3 Google Gemini 2.5 Computer Use (Oct 2025)
- **Model**: `gemini-2.5-computer-use-preview-10-2025` (Vertex AI / AI Studio).
- **Benchmarks**: outperforms competitors on Browserbase / Online-Mind2Web, lowest latency (browser-first).
- **Project Astra**: research umbrella (universal assistant). Astra now powers Search, Gemini app, dev APIs (May 2025).
- **Training disclosed**: minimal — but obvious heavy use of synthetic trajectories + Gemini 2.5 Pro multimodal backbone.
- URL: https://blog.google/innovation-and-ai/models-and-research/google-deepmind/gemini-computer-use-model/

---

## 2. Open-Source Computer-Use Models (the actual targets to study/copy)

### 2.1 OpenCUA (xlang-ai, 2025) — **THE open foundation**
- **Repo**: https://github.com/xlang-ai/OpenCUA ; site: https://opencua.xlang.ai/
- **Paper**: arxiv 2508.09123 (NeurIPS 2025 Spotlight).
- **AgentNet dataset**: **22,600 task demos** across Windows + macOS + Ubuntu, 200+ apps/sites — released open.
- **Method**:
  1. Capture human computer-use demos via annotation infra.
  2. Convert to (state, action) pairs with **reflective long CoT inner monologue** at 3 levels: high-level observation → reflective thoughts → executable action.
  3. Train on Kimi-VL-A3B / Qwen2-VL-7B / Qwen2.5-VL-7B / Qwen2.5-VL-32B.
- **Benchmarks**:
  - OpenCUA-72B: **45.0% OSWorld-Verified** (open-source SOTA at release)
  - OpenCUA-7B: ~30% OSWorld (rough — see leaderboard)
  - **60.8% ScreenSpot-Pro**, **37.4% UI-Vision** SOTA
- **License**: MIT (code) + dataset CC. **THIS IS THE REFERENCE RECIPE.**
- **Wire-into-V14**: Skip vision tower — use the **CoT + action schema** as text-only training.

### 2.2 UI-TARS / UI-TARS-2 (ByteDance Seed, Jan 2025 / Sep 2025)
- **Paper**: https://arxiv.org/abs/2501.12326 ; UI-TARS-2 (Sep 2025): https://arxiv.org/abs/2509.02544
- **Approach**: Pure-vision (screenshots only) → human-like keyboard/mouse output.
- **Base**: Qwen-2-VL fine-tuned on **~50B tokens** of GUI-specific data.
- **Variants**: UI-TARS-1.5-7B (open Apr 2025 on HF), UI-TARS-2 (multi-turn RL, hybrid env with files/terminals).
- **Benchmarks**:
  - UI-TARS-2: **47.5% OSWorld** (SOTA open at release)
  - SOTA on 10+ GUI agent benchmarks (perception/grounding/execution)
- **Key insight**: **multi-turn RL after SFT** is the lever 30%→47%.
- **Wire-into-V14**: Adopt the action vocabulary; if going text-only, use UI-TARS' tool-call format.
- HF: https://huggingface.co/ByteDance-Seed/UI-TARS-1.5-7B

### 2.3 Fara-7B (Microsoft, Nov 2025) — **most relevant for Surrogate**
- **Paper**: arxiv 2511.19663 ; https://huggingface.co/microsoft/Fara-7B
- **Why critical**: 7B parameter, **screenshot-only at runtime** (no accessibility tree), beats UI-TARS-1.5-7B AND prompted GPT-4o.
- **FaraGen pipeline** (training data):
  - **145,603 verified browser trajectories**
  - **1,010,797 steps** generated by multi-agent task proposal + solving + LLM verifier
  - across **70,117 domains** on live websites
  - Public release: open weights + paper + recipe.
- **Efficiency**: ~16 steps/task vs UI-TARS-1.5's ~41 → trained for *brevity*.
- **Architecture insight**: trained on accessibility-tree-rich data but **inference uses screenshots only** → "training proxy" matters more than runtime modality.
- **License**: open-weight (check HF card for commercial terms).
- **Wire-into-V14**: Closest reference for "small text-only model trained from accessibility traces" — Surrogate can copy the *training-data side* even if inference stays DOM-as-text.

### 2.4 Aguvis / ShowUI (2024)
- **Aguvis**: pure-vision GUI agent, 2-stage pipeline (grounding+reasoning → planning), inner-monologue, no closed-source teachers.
- **ShowUI**: enhances Qwen2-VL-2B with UI-guided token selection — **2B small enough for T4 single GPU**.
- Strong starting points if going small-VLM route.

### 2.5 PC Agent-E (2025) — WindowsAgentArena-V2
- Built on Qwen2.5-VL-72B base.
- **141% relative gain** over base via task-specific training.
- Beats Claude 3.7 Sonnet w/ extended thinking on WAA-V2.
- Demonstrates: with right data, post-training can close gap to Claude 3.x even at 72B open base.

### 2.6 MolmoWeb (2025)
- Fully open multimodal web agents.
- **MolmoWebMix**: 100K+ synthetic trajectories + 30K+ human demos. Permissive license target.

---

## 3. Foundation VLMs to potentially swap-in for Qwen2.5-Coder-7B base

| Model | Size | Released | OSWorld | ScreenSpot-Pro | T4×2 fit? | License |
|-------|------|----------|---------|----------------|-----------|---------|
| **Qwen2.5-VL-7B** | 7B | Jan 2025 | mid-teens zero-shot, 79.6% screen-grounding w/ 1-2k GRPO RL | mid 30s | borderline (24GB AWQ) | Apache-2.0 |
| **Qwen3-VL-8B** | 8B | late 2025 | better than 2.5 on agent tasks | strong | borderline | Apache-2.0 |
| **Qwen3-VL-32B** | 32B | 2025 | 61.8% ScreenSpot-Pro | 61.8 | NO (T4×2 too small) | Apache-2.0 |
| **InternVL3-8B / 3.5-8B** | 8B | Apr/Aug 2025 | strong agentic | n/a | borderline | MIT |
| **InternVL3-78B** | 78B | 2025 | 72.2 MMMU SOTA open | n/a | NO | MIT |
| **Pixtral-12B** | 12B | Sep 2024 | n/a (general VLM) | n/a | NO single T4 | Apache-2.0 |
| **Phi-4-Multimodal** | 5.6B | Mar 2025 | n/a (vision+speech+text mix-LoRA) | n/a | YES (T4 16GB ok int8) | MIT |
| **Llama-3.2-11B-vision** | 11B | Oct 2024 | n/a | n/a | borderline | Llama license |
| **Llama-4-Scout** | 17B-MoE | Apr 2025 | "natively multimodal", H100-class | n/a | NO | Llama license |
| **CogVLM2-19B** | 19B | 2024 | open + Llama3-8B | n/a | NO | Apache-ish |
| **Idefics3-8B** | 8B | 2024 | based on Llama3.1 + SigLIP | n/a | borderline | Apache-2.0 |
| **SmolVLM** | 2B | 2024 | small open | n/a | YES | Apache-2.0 |
| **Kimi-VL-A3B** | A3B-MoE | 2024 | OpenCUA base | n/a | TBD | open |

### 3.1 Key recipe: Qwen2.5-VL-3B + GRPO RL (Jan 2025 paper)
- **Trick**: 1-2k self-supervised GUI transition pairs + GRPO RL → **79.6% screen grounding**, **85.4% AndroidControl-Low**.
- Beats SFT baselines by **+47 points** vs zero-shot Qwen2.5-VL-3B.
- **Lesson**: small RL data + GRPO trumps massive SFT for grounding.
- Fits T4×2 if 3B + LoRA used.

### 3.2 LoRA on vision tower
- Industry pattern (2024-2025): freeze vision encoder, LoRA on language attention (q_proj/v_proj) + projection adapter only.
- Fine-tune Qwen2.5-VL-7B in **~2.3h for 3 epochs on 1.7k samples, peak 24GB VRAM** at 1024px input — published recipes.
- **VLA (robotics) precedent**: 3.1B parameter VLA on **8GB consumer GPU** via LoRA+quant — proves T4×2 has room.

---

## 4. Datasets — Computer Use & GUI Trajectories

### 4.1 Trajectory & action datasets
| Dataset | Size | Modality | License | Source |
|---------|------|----------|---------|--------|
| **AgentNet (OpenCUA)** | 22.6k demos, 200+ apps, 3 OS | screenshot+actions+CoT | open (CC) | xlang-ai |
| **FaraGen-traces (Fara-7B)** | 145.6k traj, 1M+ steps, 70k domains | accessibility+screenshot+actions | open weights, dataset partial | Microsoft |
| **MolmoWebMix** | 100k synthetic + 30k human | screenshot+action | permissive (verify) | Allen AI |
| **OS-Genesis** | reverse-task synth | trajectory pairs | ACL 2025 release | open |
| **AgentTrek** | scalable synth from web tutorials | trajectories | open | OpenReview EEgYUccwsV |
| **UniTraj** | 5 platforms, 11 actions, ~5.6 step avg | unified GUI corpus | open | pretraining repo |
| **Mind2Web** | 2,350 tasks, 137 sites, 31 domains | text + screenshot | open (NeurIPS 2023) | OSU-NLP |
| **Multimodal-Mind2Web** | + screenshots | + image | open | OSU-NLP |
| **Online-Mind2Web** | 300 live tasks, 136 sites | live web | open eval | OSU-NLP |
| **ScribeAgent traces** | "billions" user-browser, 250+ domains | DOM+actions | partial open | ScribeAgent |

### 4.2 Web-page → code datasets (for UI understanding & layout)
| Dataset | Size | Modality | License |
|---------|------|----------|---------|
| **WebSight v0.2** | **2M** screenshot↔HTML+Tailwind pairs | image+HTML | open (HF, Apache-ish) |
| **Web2Code** | large webpage→code | image+HTML | NeurIPS 2024 D&B |
| **WebCode2M** | 2.56M real-world | image+HTML | open |

### 4.3 Doc retrieval (vision-as-doc)
| Resource | Method | Embed size | License |
|----------|--------|-----------|---------|
| **ColPali** | PaliGemma-3B + multi-vec ColBERT-style | per-patch | Apache-2.0 |
| **ColQwen2-2B** | Qwen2-VL-2B base, **+5.3 nDCG@5 over ColPali** | per-patch (variable res) | Apache-2.0 |
| **ViDoRe** | Eval benchmark | retrieval | open |
| **Docmatix** | doc training data, **240×** prior | image+text | open |

### 4.4 GUI grounding benchmarks (target metrics)
- **ScreenSpot** — basic grounding
- **ScreenSpot-Pro** — 1581 pairs, 23 pro apps, 5 domains, 3 OS. 7B SOTA: **SE-GUI 47.2%** (3k samples open!) ; OS-Atlas-7B 18.9%, UGround-7B 16.5%.
- **OSWorld / OSWorld-Verified** — 369 tasks, real Ubuntu/Windows/macOS.
- **OSWorld-G** — grounding-specific, NeurIPS 2025 Spotlight (xlang-ai).
- **WindowsAgentArena (V1/V2)** — 150+ Windows tasks. Navi 19.5%, PC-Agent-E SOTA.
- **WebArena / VisualWebArena** — sandboxed websites.
- **WebVoyager** — live web, 87% ceiling Operator.
- **AppWorld** — 750 tasks, 9 apps, 457 APIs, GPT-4o **49% normal / 30% challenge** (ACL'24 Best Resource).
- **AndroidWorld** — mobile.
- **UI-Vision** — UI understanding.

---

## 5. Text-Only Computer-Use Proxies (cheapest path for Surrogate)

> **The big insight**: V14's Qwen2.5-Coder-7B base is text-only. Adding vision = swap base ($$$ cost). But **most "computer use" can be done text-only via DOM/HAR/AT proxies**.

### 5.1 Accessibility tree as text — the strongest proxy
- **Definition**: ARIA-flavored simplified semantic tree (what screen-readers consume).
- **Key advantage**: 10-100x smaller than raw DOM, contains *only interactive + role-labeled* elements.
- **Fara-7B uses AT during training, screenshots at inference** — proves AT is rich enough to learn from.
- **WorkArena uses AT as agent observation** → cleaner space than DOM/pixels.
- **For Surrogate**: emit `click(node_id=42)` against numbered AT nodes — pure text, fits Qwen2.5-Coder-7B perfectly.

### 5.2 DOM-as-text with downsampling
- Raw DOM = 1M+ tokens common → unusable.
- **D2Snap (Aug 2025)** — downsample DOM keeping inherent UI features; trains agents on consolidated representation.
- **Browser-Use lib** (78k+ stars MIT) — already structures DOM into LLM-friendly numbered interactive elements; pairs with Playwright. Effectively a DOM→text→action runtime — copy its observation format for training.

### 5.3 HAR / network-trace as text
- HTTP Archive logs of user sessions = full request/response corpus.
- For backend-API workflows (REST), HAR is more complete than DOM — model emits `POST /api/v1/x` instead of clicks.
- **AppWorld pattern**: 457 APIs across 9 apps → train pure-text function-call agent → 30-49% solve rate at GPT-4o level.

### 5.4 Markdown / HTML simplification
- Simple readability-style strip → markdown → LLM-friendly.
- Loses interactive bindings but works for read-only research/scrape tasks.

### 5.5 Hybrid: DOM-as-text + ColQwen2 retrieval (NO base swap needed)
- Surrogate stays Qwen2.5-Coder-7B.
- For doc-heavy tasks: ColQwen2 (separate small VLM) embeds page screenshots, returns text snippets.
- Surrogate operates on text. **Cleanest split** between perception (offload to ColQwen2) and action (Qwen2.5-Coder-7B).

---

## 6. Practical Wiring into V14+ Surrogate

### 6.1 Three architecture options

**Option A — Stay text-only (RECOMMENDED, cheapest)**
- Base: Qwen2.5-Coder-7B (no swap)
- Observation: accessibility tree (numbered) + URL + page title (text)
- Action: tool calls (`click(id)`, `type(id, text)`, `key(combo)`, `scroll(dir)`, `goto(url)`)
- Training data: AgentNet + FaraGen + Mind2Web — convert to AT-text + actions only
- Cost: T4×2 LoRA fits, ~24h training on 100k traces
- **OSWorld realistic ceiling: ~25-35%** (Browser-Use / ScribeAgent-class), matches AppWorld text-only proven results
- **Data work**: write a converter from screenshot+CoT to AT-text+CoT (one-time engineering investment, reuses existing datasets)

**Option B — Swap base to Qwen2.5-VL-7B / Qwen3-VL-8B**
- Lose code-specialty unless do mid-training merge.
- Gain: pixel grounding for native apps, PDF screenshots, visual canvases.
- VRAM: 24GB+ needed — **T4×2 (16GB×2) marginal**, requires AWQ INT4 + offload + small batch. Doable but tight.
- Training data: full OpenCUA pipeline.
- **OSWorld ceiling: 30-45%** (UI-TARS-1.5-7B / Fara-7B-class).
- **Cost**: 4-10x Option A, needs custom kernel/quant work.

**Option C — Hybrid: Qwen2.5-Coder-7B + ColQwen2-2B side car**
- Qwen2.5-Coder-7B emits `vision_query("what does this region show")` calls.
- ColQwen2-2B (~4GB) processes screenshot → returns OCR + caption + grounded text.
- Surrogate operates on ColQwen2's text output.
- **Ceiling**: matches Option A on agent tasks, but unlocks doc-retrieval (ViDoRe-style).
- **Data**: train Qwen2.5-Coder-7B on tool-call traces where vision_query results are inlined.

### 6.2 Recommended path for V14+
- **Phase 1 (V14)**: Option A — text-only computer use. Train on FaraGen-style + AgentNet AT-converted data. Target: **20-30% OSWorld**, **40-60% AppWorld**, **50-70% WebArena-text-only subset**.
- **Phase 2 (V14.5)**: bolt on ColQwen2 sidecar (Option C). Unlock document/PDF tasks without base swap.
- **Phase 3 (V15+)**: only if budget allows — switch to Qwen3-VL-8B (when better hardware/cloud), full OpenCUA pipeline. Target: **35-45% OSWorld**.

### 6.3 T4×2 feasibility matrix
| Approach | VRAM peak | Training feasibility | Inference feasibility |
|----------|-----------|----------------------|-----------------------|
| Qwen2.5-Coder-7B + LoRA + AT-text | 12-14GB | YES Kaggle T4×2 free | YES single T4 |
| Qwen2.5-VL-3B + LoRA + GRPO | 14-16GB | YES T4×2 tight | YES |
| Qwen2.5-VL-7B + QLoRA + 1024px | 22-24GB | NO single T4 ; YES T4×2 w/ ZeRO offload | YES T4×2 |
| Qwen3-VL-8B + LoRA | ~26GB | NO T4×2 ; YES Lightning H200 single | Needs ≥24GB |
| Qwen2.5-VL-32B / Qwen3-VL-32B | 60GB+ | NO any free tier | NO |
| OpenCUA-72B replication | 144GB+ | NO without paid cluster | NO |

**Verdict**: 7B-class **30%+ OSWorld is reproducible** (Fara-7B / UI-TARS-1.5-7B / OpenCUA-7B existence proofs), but **only by switching base to a VL model** OR going text-only-AT proxy. Pure Qwen2.5-Coder-7B with no base change → ceiling ~25% OSWorld via AT-text proxy. That's the honest realistic answer.

---

## 7. Training Recipes — Key Levers

### 7.1 SFT on inner-monologue CoT (OpenCUA recipe)
- Format: `<observation> → <reflective_thoughts> → <plan> → <executable_action>`
- Three-tier nesting prevents thought collapse.
- Scales linearly with data volume.

### 7.2 RL on agentic trajectories (UI-TARS-2, ARPO, multi-turn)
- **GRPO** (Group Relative Policy Optimization) — most data-efficient for grounding (Qwen2.5-VL-3B 79.6% on 1-2k pairs).
- **ARPO** (replay-buffer + task selection) — 29.9% OSWorld on 7B-class.
- **Multi-turn RL** (UI-TARS-2) — closes 30→47% gap on OSWorld.
- **Reward signal**: state-based unit tests (AppWorld pattern) > human pref > reasoning-only.

### 7.3 Synthetic trajectory generation (cheap data scaling)
- **AgentTrek**: web tutorials → trajectories (cost ~$0.28/successful traj, **Explorer**).
- **FaraGen**: multi-agent propose+solve+verify → 145k traj on 70k domains.
- **OS-Genesis**: reverse task synthesis from random actions.
- **For Surrogate**: synthesize AT-action traces from web tutorials + LLM verification — fits free tier easily.

### 7.4 Curriculum / data mix
- OpenCUA pattern: pre-train screen-text understanding → SFT on action traces → RL on hard tasks.
- Dataset blend: 60% web (Mind2Web/FaraGen) + 30% desktop (AgentNet/AssistGUI) + 10% mobile (AndroidControl).
- Augment with WebSight-v0.2 for layout-priors (image-free version uses HTML structure only — perfect for text-only Surrogate).

### 7.5 Vision-tower LoRA (if Option B)
- Freeze ViT.
- LoRA on language q_proj/v_proj + connector adapter.
- Rank 32-64 typical.
- 1024px input, gradient accumulation, ZeRO-2 offload.
- ~2.3h on 1.7k samples per published recipe.

---

## 8. Cheap-Path Rec: "Surrogate sees and clicks" Concrete Plan

```
Stage 1 (V14 — 4-6 weeks effort, $0 hardware)
  Base:      Qwen2.5-Coder-7B (unchanged)
  Modality:  text — accessibility-tree-as-text + URL + viewport summary
  Actions:   Browser-Use action schema (click/type/scroll/key/goto/wait)
  Data:      AgentNet (CC) + FaraGen-derived AT traces + Mind2Web-text
             + AppWorld function-call traces
  Training:  LoRA on T4×2 Kaggle, 50k traces × 3 epochs
  Eval:      AppWorld (target 35%+), Mind2Web-offline (40%+),
             WebArena-text-subset (~25%)
  Inference: Playwright sidecar emits AT, model emits actions

Stage 2 (V14.5 — adds doc reading, ~2 weeks)
  Add: ColQwen2-2B as "vision_query" tool
  Train on: tool-call traces inlining ColQwen2 results
  Eval: ViDoRe + DocVQA, no agentic regress
  Cost: +4GB VRAM at inference, fits T4

Stage 3 (V15+ — only if hardware budget grows)
  Swap base → Qwen3-VL-8B
  Full OpenCUA pipeline (CoT inner monologue + GRPO RL)
  Eval target: 35-45% OSWorld
  Cost: H100/H200 cluster needed for training
```

### Why this is the right call for Surrogate:
1. **No base swap** = preserves 76.2% SWE-Bench coding strength of Qwen2.5-Coder.
2. **AT-text proxy proven** by Fara-7B/Browser-Use existing systems.
3. **T4×2 free** = $0 training cost.
4. **Permissive datasets** (Mind2Web NeurIPS, AgentNet CC, AppWorld ACL) — clean license trail.
5. **Surrogate uses Playwright as runtime** — already standard Python; no native-app ML stack required.
6. **Capability ceiling realistic** — matches what 7B-class achieves text-only (Fara is essentially this approach with vision dropped at inference).

---

## 9. Permissive-License Datasets — Quick Reference for V14

| Dataset | License | Modality | Use |
|---------|---------|----------|-----|
| **Mind2Web / Multimodal-Mind2Web / Online-Mind2Web** | open NeurIPS | text+image | web action SFT |
| **AgentNet (OpenCUA)** | CC + MIT code | screenshot+action+CoT | full agent SFT |
| **WebSight v0.2** | open HF (verify Apache) | screenshot+HTML | layout priors |
| **Web2Code** | NeurIPS 2024 D&B | webpage+code | UI→code SFT |
| **WebCode2M** | open | webpage+code | UI→code SFT |
| **AppWorld** | ACL 2024 Best Resource (open) | text+API traces | function-call SFT |
| **WindowsAgentArena V1/V2** | MIT | image+action | desktop agent eval/train |
| **OSWorld / OSWorld-Verified / OSWorld-G** | open NeurIPS | image+action+state | benchmarks (eval only) |
| **OS-Genesis traj** | open ACL 2025 | image+action | trajectory SFT |
| **MolmoWebMix** | open (verify) | image+action | web SFT |
| **WebArena** | open Apache | sandbox env | eval/train env |
| **Docmatix** | open (HF) | image+text | doc understanding |
| **ViDoRe** | open | image | retrieval eval |
| **Browser-Use traces** | MIT lib | DOM+action | RAW data via Playwright runs |

---

## 10. Open Questions / Risks for V14

- **Qwen2.5-Coder-7B has zero vision tokens** — text-only at inference is the only realistic V14 path. Vision at training ≠ vision at inference (Fara-7B proves this works).
- **Accessibility tree quality varies by site** — modern SPA (React) sometimes produces poor AT; need fallback to DOM-text.
- **Dataset license diligence**: Mind2Web cached websites under fair use; FaraGen/AgentNet are clean. WebSight uses synthetic + real images — verify v0.2 license before commercial deploy.
- **CC-license caveat**: copyright OK for AI training but personal-data/privacy not covered (CC explicit).
- **OSWorld ceiling for text-only**: untested in literature — best estimate 20-30%. Fara-7B at runtime is screenshot-only but trained on AT. Pure-AT inference may be lower.
- **Tool-call schema lock-in**: Standardize on Browser-Use schema or invent custom. Custom = more flexibility but more eval surface to maintain.
- **Surrogate is a coding agent first** — multimodal addition must NOT regress coding/SWE-Bench. Use mix-LoRA or modular adapter rather than full fine-tune.

---

## 11. Sources / Further Reading (2024-2026)

### Frontier
- Anthropic Computer Use: https://www.anthropic.com/news/3-5-models-and-computer-use
- Anthropic Vercept acquisition: https://www.techzine.eu/news/analytics/139119/
- Claude Opus 4.5 system card: https://www.anthropic.com/claude-opus-4-5-system-card
- OpenAI Operator/CUA: https://openai.com/index/computer-using-agent/
- OpenAI CUA sample app: https://github.com/openai/openai-cua-sample-app
- Gemini 2.5 Computer Use: https://blog.google/innovation-and-ai/models-and-research/google-deepmind/gemini-computer-use-model/
- Project Astra: https://deepmind.google/models/project-astra/

### Open Computer-Use Models
- OpenCUA: https://github.com/xlang-ai/OpenCUA ; https://opencua.xlang.ai/ ; arxiv 2508.09123
- UI-TARS: https://arxiv.org/abs/2501.12326 ; https://huggingface.co/ByteDance-Seed/UI-TARS-1.5-7B
- UI-TARS-2: https://arxiv.org/abs/2509.02544
- Fara-7B: arxiv 2511.19663 ; https://huggingface.co/microsoft/Fara-7B ; https://github.com/microsoft/fara
- ScribeAgent: see arxiv listings 2024
- Browser-Use: https://github.com/browser-use/browser-use

### VLM Bases
- Qwen3-VL: https://github.com/QwenLM/Qwen3-VL ; arxiv 2511.21631
- Qwen2.5-VL: https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct
- InternVL3: https://internvl.github.io/blog/2025-04-11-InternVL-3.0/
- InternVL3.5: https://internvl.github.io/blog/2025-08-26-InternVL-3.5/
- Pixtral: https://mistral.ai/news/pixtral-large
- Phi-4-Multimodal: https://huggingface.co/microsoft/Phi-4-multimodal-instruct ; arxiv 2503.01743
- Llama 3.2 Vision: https://ai.meta.com/blog/llama-3-2-connect-2024-vision-edge-mobile-devices/
- Llama 4: https://ai.meta.com/blog/llama-4-multimodal-intelligence/
- CogVLM2: https://github.com/zai-org/CogVLM2
- Idefics3: HF blogs

### Datasets & Benchmarks
- Mind2Web: https://osu-nlp-group.github.io/Mind2Web/
- WebArena / WebVoyager / Online-Mind2Web — see "Illusion of Progress" arxiv 2504.01382
- OSWorld: https://github.com/xlang-ai/OSWorld ; OSWorld-Verified blog https://xlang.ai/blog/osworld-verified
- OSWorld-G: https://github.com/xlang-ai/OSWorld-G
- WindowsAgentArena: https://microsoft.github.io/WindowsAgentArena/
- AppWorld: https://appworld.dev/ ; https://github.com/StonyBrookNLP/appworld
- WebSight: https://huggingface.co/blog/websight ; arxiv 2403.09029
- Web2Code / WebCode2M: NeurIPS 2024 papers
- ColPali / ColQwen2: https://github.com/illuin-tech/colpali
- ScreenSpot-Pro: https://github.com/likaixin2000/ScreenSpot-Pro-GUI-Grounding
- UGround: https://github.com/OSU-NLP-Group/UGround
- AgentTrek / OS-Genesis / MolmoWeb — see GUI-Agents-Paper-List by OSU-NLP-Group
- GUI Agents Paper List: https://github.com/OSU-NLP-Group/GUI-Agents-Paper-List
- Awesome GUI Agent: https://github.com/showlab/Awesome-GUI-Agent

### Training Methodology
- LoRA Qwen2.5-VL fine-tune: https://heyyanshuman.com/posts/fine-tuning-vlm
- Efficient VLM fine-tuning survey: arxiv 2504.09724
- D2Snap (DOM downsampling): arxiv 2508.04412
- Beyond Pixels DOM survey: arxiv 2506.10953

---

**See also**: [[v13-frontier-capability]] · [[v13-multi-agent-baked-in]] · [[training-tooling-2026-Q2]] · [[opensource-releases-2026-Q2]] · [[autonomous-24x7]]
