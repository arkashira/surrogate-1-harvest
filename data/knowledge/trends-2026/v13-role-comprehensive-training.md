---
title: "V13 Role-Comprehensive Training — 30+ SDLC + Business + Marketing Hats in One Model"
date: 2026-05-01
project: surrogate-1
version: V13
purpose: "Train ONE model to switch into any of 30+ professional roles via system-prompt activation. Cover full org chart from CEO/PM/PO/SA/BD → all engineering disciplines → DevSecOps/SRE → all QA → marketing/growth/PMM → tech writing/EM."
tags: [v13, role-training, persona, multi-role, sdlc, sft, lora-moe, persona-hub, role-routing]
---

## Why V13 Goes Multi-Role

V12 nailed code + ops via single-domain SFT. V13 needs to wear 30+ hats — every role on a modern product team — without 30 separate models. The frontier consensus (Anthropic PSM, Tencent PersonaHub, Allen AI Tülu 3) is that a single base model already contains every role latently from pretraining; post-training **elicits, refines, and sharpens** the activation surface so role X gets reliably reproduced when triggered by a system prompt.

Three composable techniques drive V13:
1. **Persona-conditioned SFT** — every training example tagged with `<role>` + system prompt that activates that role
2. **LoRA-per-role with MoLE/X-LoRA composition** — heavy roles get their own adapter; gating router picks 1-3 to compose at inference
3. **Cross-role multi-turn** — chain examples (PM writes PRD → SA reviews → Eng implements → QE tests → SRE runbook → PMM positions) so model learns hand-offs

> **Anthropic's Persona Selection Model (PSM, Mar 2026)**: LLMs learn to simulate diverse characters during pre-training; post-training elicits and refines a particular Assistant persona. Upsampling descriptions of role-X behavior in pre/mid-training shifts the post-trained model toward role X. ([alignment.anthropic.com](https://alignment.anthropic.com/2026/psm/))

This validates "soft activation via system prompt" instead of hard MoE branching. We use both: SFT shifts the prior; LoRA-MoE provides hard capacity for the heavy specialty roles (Frontend, Backend, SRE, ML).

---

## Public Corpora — Inventory by Role Family

### A. Engineering Roles (SDLC / Code)

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 1 | **The Stack v2** ([bigcode-project/the-stack-v2](https://github.com/bigcode-project/the-stack-v2)) | Permissive (MIT/Apache only) | 6.4 TB / 358 langs | Frontend/Backend/Mobile/Data — base code corpus, opt-out compliant, commercially defensible |
| 2 | **StarCoderData** ([bigcode/starcoderdata](https://huggingface.co/datasets/bigcode/starcoderdata)) | OpenRAIL-M v1 + permissive sources | 783 GB code + 54 GB GitHub Issues + 32 GB commits | Issues = role-conditioned tickets (PM ↔ Eng); commits = "what changed and why" |
| 3 | **CodeReviewer dataset** (Microsoft, [arxiv 2203.09095](https://arxiv.org/pdf/2203.09095)) | Research, GitHub-derived | 25.3M PR review comments | Senior eng / Reviewer / Principal Eng persona — "review-style" turn |
| 4 | **SWE-bench (+Verified, +Pro, +smith)** ([swebench.com](https://www.swebench.com/)) | MIT (research) | 2,294 + 500 + Pro = ~3K verified issue→patch pairs | Backend/Full-stack debug persona, multi-step agentic |
| 5 | **CommitPackFT** (BigCode, derived from Stack) | Permissive | 2M commits with messages | Eng "explain my change" persona |

### B. Architecture / Solutions / Principal Eng

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 6 | **MADR + adr.github.io examples** ([github.com/adr/madr](https://github.com/adr/madr)) | CC0 / public domain | ~10K ADRs across public repos (scrape via GitHub search `path:docs/adr extension:md`) | Solutions Architect persona, decision records |
| 7 | **RFC corpus from Pragmatic Engineer list** ([blog.pragmaticengineer.com/rfcs-and-design-docs](https://blog.pragmaticengineer.com/rfcs-and-design-docs/)) | Mixed; cite source | Public RFCs from Stripe, Cloudflare, Pulumi, Squarespace, etc. | Principal Eng / Tech Lead persona |
| 8 | **IETF RFC archive** ([rfc-editor.org](https://www.rfc-editor.org/)) | Trust, freely usable | 9000+ RFCs | Network architect persona |
| 9 | **AWS Architecture Center + GCP Cloud Architecture Center** | Public | Hundreds of reference architectures | Cloud SA persona |
| 10 | **The Pragmatic Engineer / Marc Brooker / High Scalability** | Mixed (cite + paraphrase) | ~1500 articles | System design narrative style |

### C. DevSecOps / SRE / Platform

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 11 | **Google SRE Book + Workbook** ([sre.google/sre-book](https://sre.google/sre-book/table-of-contents/) / [workbook](https://sre.google/workbook/index/)) | CC BY-NC-ND (free read; for SFT use derivatives + paraphrase) | 2 books | Postmortem/SLO/error budget persona |
| 12 | **Building Secure and Reliable Systems** ([Google SRE](https://sre.google/resources/)) | CC | 1 book | DevSecOps persona |
| 13 | **HowTheySRE** ([upgundecha/howtheysre](https://github.com/upgundecha/howtheysre)) | Permissive curation, links external | 100+ company blog posts | "Public postmortem" voice |
| 14 | **Awesome-Runbook** ([runbear-io/awesome-runbook](https://github.com/runbear-io/awesome-runbook)) | Aggregator | 50+ runbook repos | Runbook authoring persona |
| 15 | **GitLab Runbooks docs** | MIT | Public | Operational procedure persona |
| 16 | **Public postmortem archives** (Cloudflare, GitHub, AWS PHD, Atlassian StatusPage) | Citable, paraphrase for SFT | ~500 high-quality postmortems | Incident commander persona |

### D. QA / SDET / Performance / Security Testing

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 17 | **SWE-smith-traj + SWE-bench failure modes** | MIT | Tens of thousands of test traces | SDET "test-first" persona |
| 18 | **Cucumber feature-file corpus** (mine GitHub `extension:feature`) | Per-repo (mostly MIT) | ~20K Gherkin scenarios | BDD/SDET persona |
| 19 | **OWASP Top-10 / CWE writeups** | CC | Hundreds | Security tester persona |
| 20 | **CVE descriptions + exploit DB** (NVD JSON feed) | Public domain | 200K+ entries | Security analyst persona |
| 21 | **Locust/k6/JMeter examples + reports** | OSS | GitHub mining | Performance tester persona |

### E. Product Management / PO / BA

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 22 | **Reforge artifacts + ProductSchool blog** ([reforge.com/artifacts](https://www.reforge.com/artifacts/c/product-development/product-requirement-document-prd)) | Reference / paraphrase | Hundreds of PRD examples | Product Manager persona |
| 23 | **Atlassian Product Mgmt guides** | Citable | Free | PM/PO methodology persona |
| 24 | **Public PRDs scraped from GitHub** (search `"Product Requirements Document"` filename / heading in markdown) | Per-repo | Thousands | Real PRD voice |
| 25 | **JTBD framework writeups** (Strategyn, Ulwick, Christensen) | Cite | Free articles | BA/PM "outcome-driven" persona |
| 26 | **Atlassian/Aha! BRD templates** | Free templates | Dozens | Business Analyst persona |
| 27 | **Project Management LLM Dataset** ([ai-in-projectmanagement/ProjectManagementLLM_dataset](https://huggingface.co/datasets/ai-in-projectmanagement/ProjectManagementLLM_dataset)) | HuggingFace | Synthetic | PM tabular reasoning |

### F. Business Development / Sales / Customer Success

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 28 | **Bitext customer-support LLM dataset** ([bitext/Bitext-customer-support-llm-chatbot-training-dataset](https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset)) | CDLA-Sharing 1.0 (commercial-clean) | 27K Q/A, 3.57M tokens | Customer Success / Support persona |
| 29 | **goendalf666 sales-conversations** ([HF](https://huggingface.co/datasets/goendalf666/sales-conversations)) | Apache 2.0 | Synthetic, multi-turn | BD / Sales Engineer persona |
| 30 | **DeepMostInnovations saas-sales-conversations** ([HF](https://huggingface.co/datasets/DeepMostInnovations/saas-sales-conversations)) | Apache 2.0 | Synthetic | SaaS BD persona |
| 31 | **TWEETSUMM** ([aclanthology.org/2021.findings-emnlp.24](https://aclanthology.org/2021.findings-emnlp.24/)) | Research/CC | 6500 dialogs | CS persona |
| 32 | **Syncora customer_support_conversations_dataset** | CC | Synthetic | CS multi-industry |

### G. Marketing / Growth / PMM / SEO / Brand

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 33 | **smangrul/ad-copy-generation** ([HF](https://huggingface.co/datasets/smangrul/ad-copy-generation)) | Apache 2.0 | Ad copy pairs | Copywriter persona |
| 34 | **RafaM97/marketing_social_media** ([HF](https://huggingface.co/datasets/RafaM97/marketing_social_media)) | CC | Marketing campaign data | Growth persona |
| 35 | **Ateeqq/Title-Keywords-SEO** ([HF](https://huggingface.co/datasets/Ateeqq/Title-Keywords-SEO)) | Open | SEO title↔keyword | SEO persona |
| 36 | **PeterBrendan/Ads_Creative_Text_Programmatic** ([HF](https://huggingface.co/datasets/PeterBrendan/Ads_Creative_Text_Programmatic)) | Open | Programmatic ad copy | Ads persona |
| 37 | **Common Corpus (PleIAs)** ([HF](https://huggingface.co/datasets/PleIAs/common_corpus)) | Public domain | 500B words | Brand/positioning corpus base |
| 38 | **VPPC video-product-promotion corpus** ([sciencedirect S0925231224000249](https://www.sciencedirect.com/science/article/pii/S0925231224000249)) | Research | Multimodal | Multimodal copy persona |

### H. Methodology / Tech Writing / EM / Process

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 39 | **Stack Exchange dumps** (Workplace, PM, ServerFault, SuperUser, SoftwareEng) ([HF stack-exchange-paired](https://huggingface.co/datasets/lvwerra/stack-exchange-paired)) | CC BY-SA 4.0 | Millions Q&A | Per-tag role personas |
| 40 | **AgileScrumSprintVelocityDataSet** ([github.com/RandulaKoralage/AgileScrumSprintVelocityDataSet](https://github.com/RandulaKoralage/AgileScrumSprintVelocityDataSet)) | Public | 4 OSS projects | PM/Scrum persona signal |
| 41 | **Djinni Recruitment Dataset** ([aclanthology.org/2024.unlp-1.2](https://aclanthology.org/2024.unlp-1.2/)) | MIT | 150K jobs + 230K CVs | Hiring/EM persona |
| 42 | **GitLab Handbook** ([handbook.gitlab.com](https://handbook.gitlab.com/)) | CC BY-SA | 3000+ pages | EM / process / People Ops persona — gold standard |

### I. Persona-Driven Synthesis (force-multiplier)

| # | Source | License | Size | Relevance |
|---|--------|---------|------|-----------|
| 43 | **Tencent PersonaHub** ([github.com/tencent-ailab/persona-hub](https://github.com/tencent-ailab/persona-hub), [arxiv 2406.20094](https://arxiv.org/abs/2406.20094)) | Apache 2.0, 1B personas, 200K shipped | Use as **role-generator** to mint training pairs from any of our 30 role personas | Synthesis engine |
| 44 | **Tülu 3 SFT Personas Instruction-Following** ([allenai/tulu-3-sft-personas-instruction-following](https://huggingface.co/datasets/allenai/tulu-3-sft-personas-instruction-following)) | ODC-BY 1.0 | 29.9K verifiable persona pairs | Direct persona SFT |
| 45 | **Tülu 3 Personas Math + Code** ([HF](https://huggingface.co/datasets/allenai/tulu-3-sft-personas-math)) | ODC-BY 1.0 | 100K+ pairs | Persona-conditioned reasoning |
| 46 | **Magpie** ([arxiv 2406.08464](https://arxiv.org/html/2406.08464v1)) | Public method | 4M instructions | Self-synthesis from aligned model |
| 47 | **WildChat-4.8M** ([allenai/WildChat-4.8M](https://huggingface.co/datasets/allenai/WildChat-4.8M)) | ODC-BY 1.0 | 4.8M real conversations | Tagged role mining |
| 48 | **OASST1 + OASST2** ([HF](https://huggingface.co/datasets/OpenAssistant/oasst1)) | Apache 2.0 | 161K + 128K msgs | Multi-turn conversational base |
| 49 | **RoleBench (RoleLLM)** ([github.com/InteractiveNLP-Team/RoleLLM-public](https://github.com/InteractiveNLP-Team/RoleLLM-public)) | Research | 168K samples / 100 roles | Role-conditioned tuning template — **literal blueprint** |
| 50 | **Synthetic-Persona-Chat** (Google research) ([github.com/google-research-datasets/Synthetic-Persona-Chat](https://github.com/google-research-datasets/Synthetic-Persona-Chat)) | Research | Multi-turn persona | Conversation persona base |

---

## Role-Persona Synthesis Patterns From Frontier Labs

### Pattern 1: Anthropic PSM (Persona Selection Model)
- LLM = mixture of latent characters; system prompt activates one
- Train: upsample role-X descriptions in mid-training → bias prior toward role X
- Inference: system prompt + persistent voice; drift detected via "persona vectors" (activation steering at hidden state)
- Source: [alignment.anthropic.com/2026/psm](https://alignment.anthropic.com/2026/psm/)

### Pattern 2: Tencent PersonaHub Pattern
- Persona = "A senior backend engineer at a fintech who values type safety"
- Synthesis prompt = "Given persona X, write Y" → produces tagged pair
- 1B personas yield 1B-domain coverage; we sub-sample 30 role-typed ones
- Result: 1.09M math problems → finetuned 7B Qwen → 79.4% on test
- Source: [arxiv 2406.20094](https://arxiv.org/abs/2406.20094)

### Pattern 3: RoleLLM RoleBench Pattern
- 4-stage pipeline: Profile Construction → Context-Instruct (role knowledge) → RoleGPT (style imitation) → RoCIT (Role-Conditioned Instruction Tuning)
- 168K samples / 100 roles; produces RoleLLaMA matching GPT-4 RP
- We adapt: 30 professional roles × 5K-15K samples each = 150K-450K
- Source: [arxiv 2310.00746](https://arxiv.org/abs/2310.00746)

### Pattern 4: Tülu 3 Persona-IFEval Pattern
- Persona + verifiable constraint + GPT-4o synthesis = 29.9K If-Persona-SFT pairs
- Removing these → measurable IFEval drop
- Direct fit for "PM should produce PRD with these required sections"
- Source: [allenai.org/blog/tulu-3-technical](https://allenai.org/blog/tulu-3-technical)

### Pattern 5: Magpie Self-Synthesis
- Send pre-query template alone → aligned LLM generates plausible user query → answers it
- 4M pairs; outperforms 10M Llama-3-Instruct training set on AlpacaEval
- We seed with role system prompts → role-conditioned magpie
- Source: [arxiv 2406.08464](https://arxiv.org/html/2406.08464v1)

---

## System-Prompt Format — Frontier Convention

Anthropic's leaked Claude Sonnet/Opus 4.6/4.7 prompts ([asgeirtj/system_prompts_leaks](https://github.com/asgeirtj/system_prompts_leaks)) and the official [docs](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/use-xml-tags) converge on a 5-component XML-tagged structure: **role, task, context, format, constraints**.

V13 standard role activation template:

```xml
<role>
You are a {ROLE_TITLE} with {N} years experience at {ORG_TIER}.
Your superpower: {ONE_SENTENCE_DIFFERENTIATOR}.
You optimize for: {3_PRIMARY_OBJECTIVES}.
You explicitly avoid: {3_ANTI_PATTERNS}.
</role>

<context>
{situational_context}
</context>

<task>
{user_request}
</task>

<format>
{expected_output_structure}
</format>

<constraints>
{verifiable_rules: length, sections, tone, do_not_X}
</constraints>
```

**Why this works** (per Anthropic docs): Claude was trained on XML-tagged data. Tags prevent instruction-context confusion, enable hierarchical nesting, and serve as activation anchors for persona vectors.

---

## Few-Shot vs Full SFT — When Each Wins

| Approach | Best for | V13 use |
|----------|---------|---------|
| **Zero-shot system prompt** | Light roles, role with broad pretrain coverage (Sales, Marketing copy) | Roles 22-38 (rely on prior + steering) |
| **Few-shot (3-8 exemplars in prompt)** | Roles with stylistic precision (RFC, ADR voice) | Inference-time fallback for any role |
| **Full SFT (1K-50K pairs)** | Roles with deep, idiosyncratic conventions (PM, SA, SDET, SRE) | Heavy roles 1-21 |
| **LoRA-per-role** | Roles needing capacity isolation (Frontend vs ML) | Top 8 specialty adapters |
| **MoLE / X-LoRA composition** | Tasks requiring blend (e.g. PM-writing-AI-PRD = PM + ML) | Inference router selects 1-3 adapters |

Empirically (RoleLLM, Tülu 3): **5K-15K high-quality pairs per role** is the sweet spot. Below 1K = unreliable elicitation. Above 50K = overfit voice + interference with neighbor roles.

---

## LoRA-Per-Role Composition — The MoLE/X-LoRA/LoraHub Stack

**Mixture of LoRA Experts (MoLE)** ([arxiv 2404.13628](https://arxiv.org/abs/2404.13628)):
- Each trained LoRA = one expert layer
- Learnable gating function inside each layer composes weights per-task
- Beats LoRAHub by 2.5-3.0 at 48-128 LoRA scale

**LD-MoLE (2025)** ([arxiv 2509.25684](https://arxiv.org/abs/2509.25684)):
- Token-dependent + layer-wise expert allocation
- Adaptive routing — different roles activate at different transformer depths

**DR-LoRA (Jan 2026)** ([arxiv link](https://arxiv.org/abs/2509.25684)):
- Dynamic rank growth via expert saliency scores
- Heterogeneous rank profiles — heavy roles (Backend, SRE) get rank 64; light roles (PMM copy) get rank 8

**Recommended V13 architecture:**

```
Base model (Qwen3.5-27B or Mixtral-8x7B)
  + 30 role LoRAs (rank 8-64, varying)
  + LD-MoLE router (token-dependent, layer-wise)
  + Gating top-3 (sparsely activate at most 3 role adapters)
  + Identity LoRA (always-on, encodes "be Surrogate-1")
```

Cost estimate: 30 LoRAs × avg rank 24 × ~50M params each = ~1.5B trainable params. Fits one H200 SFT run per role at 5K-15K pairs.

---

## Cross-Role Multi-Turn Pattern — The Hand-Off Chain

The unique V13 capability: not just *be* a role, but **switch coherently mid-conversation**. Train on chains:

```
TURN 1 [PM]:        writes PRD section "User wants ___, success = ___"
TURN 2 [BA]:        translates to BRD with measurable acceptance criteria
TURN 3 [SA]:        proposes architecture with 2 alternatives + ADR
TURN 4 [Principal]: reviews ADR, picks option B, lists risks
TURN 5 [Backend]:   drafts API contract + data model
TURN 6 [Frontend]:  wireframes UX states, lists component contracts
TURN 7 [SDET]:      writes Gherkin scenarios + edge cases
TURN 8 [SRE]:       writes runbook + SLOs + dashboards
TURN 9 [DevSecOps]: threat-models, lists controls
TURN 10 [TechWriter]: synthesizes into customer-facing release notes
TURN 11 [PMM]:      converts release notes to launch positioning
TURN 12 [Growth]:   designs activation experiment + funnel
TURN 13 [BD/Sales]: writes sales enablement one-pager
TURN 14 [CS]:       writes onboarding playbook
TURN 15 [EM]:       drafts retro talking points + 1:1 agenda
```

Each turn switches `<role>` block. Train ~5K such chains synthesized from a teacher model (Claude Opus / GPT-5) seeded with real PRDs from public scrape. This is the **flagship V13 capability**.

Inspired by ChatDev / MetaGPT (CEO + CPO + CTO + Programmer + Reviewer + Tester + Designer roles communicating to ship software end-to-end) — but baked into the **single-model weights** via SFT, not orchestrated externally.

References:
- [ChatDev arxiv 2307.07924](https://arxiv.org/html/2307.07924v5)
- [MetaGPT openreview VtmBAGCN7o](https://openreview.net/forum?id=VtmBAGCN7o)

---

## Skill-Routing — Auto-Detect Role From Prompt

Two complementary mechanisms:

### Mechanism A: Implicit (preferred for V13)
Soft activation via system prompt + the model's PSM ability. User passes `<role>` block; nothing else needed. Our identity LoRA enforces "always read `<role>` first."

### Mechanism B: Explicit Role Router (when no `<role>` given)
Front-door classifier (small distilled model or vLLM Semantic Router pattern, [vLLM blog Sep 2025](https://blog.vllm.ai/2025/09/11/semantic-router.html)) detects intent → injects role automatically.

**Symbolic-MoE** ([openreview RYrFUkraWM](https://openreview.net/forum?id=RYrFUkraWM)): gradient-free, text-based, fine-grained skill routing — picks role-LoRAs by skill keywords without retraining.

**Skill keywords table** for the router:

| If prompt contains... | Route to role |
|----------------------|---------------|
| "PRD", "user story", "acceptance criteria" | PM |
| "ADR", "architecture", "trade-off" | SA / Principal |
| "runbook", "incident", "SLO", "postmortem" | SRE |
| "threat model", "OWASP", "CVE" | DevSecOps / SecTester |
| "Gherkin", "Given-When-Then", "test plan" | SDET |
| "messaging", "positioning", "ICP" | PMM |
| "ad copy", "headline", "CTA" | Copywriter / Growth |
| "OKR", "north star", "quarterly plan" | EM / Founder |
| "1:1", "career growth", "PIP" | EM |
| "discovery call", "demo deck", "MEDDIC" | BD / Sales Eng |
| "onboarding", "QBR", "renewal" | CS |
| "JTBD", "outcome", "BRD" | BA |

Implementation: `~/.claude/bin/role-route.sh "$PROMPT"` → emits `<role>...</role>` prefix.

---

## merge_external() Code — Concrete Recipe

```python
# v13_merge_roles.py
from datasets import load_dataset, concatenate_datasets, Dataset
from typing import Iterable
import json, hashlib, random

ROLE_REGISTRY = {
    # role_key: (system_prompt_template, target_pair_count, weight)
    "pm":            ("templates/pm.xml",            12_000, 1.2),
    "po":            ("templates/po.xml",             6_000, 0.6),
    "ba":            ("templates/ba.xml",             6_000, 0.6),
    "sa":            ("templates/sa.xml",            10_000, 1.0),
    "principal":     ("templates/principal.xml",     10_000, 1.0),
    "frontend":      ("templates/frontend.xml",      15_000, 1.5),
    "backend":       ("templates/backend.xml",       15_000, 1.5),
    "mobile":        ("templates/mobile.xml",         8_000, 0.8),
    "data":          ("templates/data.xml",          10_000, 1.0),
    "ml":            ("templates/ml.xml",            10_000, 1.0),
    "ai_eng":        ("templates/ai_eng.xml",        10_000, 1.0),
    "devsecops":     ("templates/devsecops.xml",     12_000, 1.2),
    "sre":           ("templates/sre.xml",           12_000, 1.2),
    "platform":      ("templates/platform.xml",      10_000, 1.0),
    "cloud":         ("templates/cloud.xml",         10_000, 1.0),
    "observability": ("templates/observability.xml",  8_000, 0.8),
    "qa":            ("templates/qa.xml",             8_000, 0.8),
    "sdet":          ("templates/sdet.xml",          12_000, 1.2),
    "perf_test":     ("templates/perf_test.xml",      6_000, 0.6),
    "sec_test":      ("templates/sec_test.xml",      10_000, 1.0),
    "tech_writer":   ("templates/tech_writer.xml",   10_000, 1.0),
    "em":            ("templates/em.xml",             8_000, 0.8),
    "founder":       ("templates/founder.xml",        6_000, 0.6),
    "bd":            ("templates/bd.xml",              8_000, 0.8),
    "sales_eng":     ("templates/sales_eng.xml",      8_000, 0.8),
    "cs":            ("templates/cs.xml",              8_000, 0.8),
    "pmm":           ("templates/pmm.xml",             8_000, 0.8),
    "growth":        ("templates/growth.xml",         10_000, 1.0),
    "copywriter":    ("templates/copywriter.xml",     8_000, 0.8),
    "seo":           ("templates/seo.xml",             6_000, 0.6),
    "brand":         ("templates/brand.xml",           6_000, 0.6),
}
# Total target: ~285K role-tagged pairs

def merge_external(corpora: dict[str, Dataset]) -> Dataset:
    """
    corpora: {source_name: hf_dataset_with_msgs}
    Returns: tagged + balanced multi-role SFT dataset.
    """
    out = []
    for role, (tmpl_path, target, weight) in ROLE_REGISTRY.items():
        with open(tmpl_path) as f:
            sys_prompt = f.read()
        # 1) Mine matching pairs from public corpora using role-keyword filter
        candidates = mine_by_role(corpora, role)
        # 2) Persona-Hub synthesis to fill gap if under-target
        if len(candidates) < target:
            extra = personahub_synthesize(role, target - len(candidates))
            candidates.extend(extra)
        # 3) Tag each pair with role + system prompt
        for ex in candidates[:target]:
            out.append({
                "role": role,
                "weight": weight,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    *ex["messages"],
                ],
            })
    # 4) Mix in cross-role chains (5K)
    out.extend(synthesize_cross_role_chains(n=5_000))
    random.shuffle(out)
    return Dataset.from_list(out)
```

Pseudo `mine_by_role`: keyword match on role lexicon + filter by quality heuristic (length, code presence, conversation depth).

Pseudo `personahub_synthesize`: load PersonaHub seeds, add role suffix, prompt teacher LLM, validate output against role rubric.

Pseudo `synthesize_cross_role_chains`: take a public PRD, ask teacher to expand into 10-15 turn multi-role chain (PM → SA → Eng → QE → SRE → ...).

---

## V13 Role Mix

Final SFT mix totals ~290K role-tagged pairs + 5K cross-role chains. Each row: `{role, system_prompt, user, assistant, source, weight}`. Heavy-coverage roles (FE/BE/SRE/DevSecOps/SDET/PM) get LoRA-per-role; light roles share a "general professional" LoRA.

| # | Role | System-Prompt Snapshot | Pairs Target | Primary Public Datasets |
|---|------|------------------------|-------------|-------------------------|
| 1 | **Product Manager (PM)** | "Senior PM at top-tier SaaS. Output crisp PRDs with problem/JTBD/success metric. Cite trade-offs." | 12,000 | GitHub PRD scrape + Reforge artifacts + Tülu 3 personas-IF + PersonaHub-PM |
| 2 | **Product Owner (PO)** | "Scrum PO. Prioritize backlog by RICE/WSJF, write user stories, define DoD." | 6,000 | Stack Exchange PM tag + Atlassian docs + synthesis |
| 3 | **Business Analyst (BA)** | "Senior BA. Translate business intent → BRD with measurable acceptance criteria." | 6,000 | BRD templates scrape + JTBD writeups + synthesis |
| 4 | **Solutions Architect (SA)** | "Cloud SA, AWS Pro. Compare 2-3 architectures with cost/perf/risk; emit ADR." | 10,000 | MADR ADRs + AWS/GCP arch center + RFC archives |
| 5 | **Principal Engineer** | "Principal IC. Review designs deeply; surface 2nd-order risks and migration paths." | 10,000 | CodeReviewer 25.3M + Stripe/Cloudflare RFCs + Pragmatic Engineer corpus |
| 6 | **Frontend Engineer** | "Senior FE (React/TS). Optimize bundle, a11y, perf. Strict types, no any." | 15,000 | Stack v2 (TS/JS/React subset) + StarCoder issues + Stack Overflow JS tag |
| 7 | **Backend Engineer** | "Senior BE (Go/Python/Node). Design APIs, DB schemas, idempotent writes." | 15,000 | Stack v2 + SWE-bench + Stack Exchange backend tags |
| 8 | **Mobile Engineer** | "Senior mobile (Swift/Kotlin/RN). Battery, offline, native UI patterns." | 8,000 | Stack v2 mobile subset + StarCoder issues mobile |
| 9 | **Data Engineer** | "Senior DE. Build pipelines, model star schemas, optimize warehouse cost." | 10,000 | dbt-labs OSS + Stack Exchange DBA + Awesome-Data-Engineering |
| 10 | **ML Engineer** | "Senior ML. Train, eval, ship; care about data quality > model novelty." | 10,000 | PapersWithCode notebooks + StarCoder Jupyter + HF model cards |
| 11 | **AI Engineer (LLM Apps)** | "Build LLM apps with RAG/agents/tools. Cost-aware; cite latencies." | 10,000 | LangChain/LlamaIndex docs + Glaive function-calling + ToolMind |
| 12 | **DevSecOps** | "Shift-left security. Threat-model, scan, sign, SBOM, SLSA-3+." | 12,000 | OWASP + CVE feed + Building Secure & Reliable Systems + GitLab handbook security |
| 13 | **SRE** | "Google-style SRE. Define SLO/SLI/error budget; postmortem blameless." | 12,000 | Google SRE book/workbook + HowTheySRE + public postmortems |
| 14 | **Platform Engineer** | "Build IDPs (Backstage/Crossplane). Self-service > tickets." | 10,000 | Backstage docs + GitLab handbook + CNCF blog corpus |
| 15 | **Cloud Engineer** | "AWS/GCP/Azure. Right-size, cost-watch, IaC strict." | 10,000 | AWS/GCP architecture center + Stack v2 IaC + Terraform registry |
| 16 | **Observability Engineer** | "OTel everywhere. RED/USE metrics; alert on symptoms not causes." | 8,000 | OTel docs + Honeycomb/Grafana blogs + SRE workbook ch.4-6 |
| 17 | **QA Engineer** | "Risk-based testing. Exploratory + scripted; bug reports = repro/expected/actual." | 8,000 | Stack Exchange SQA + Bugzilla public + synthesis |
| 18 | **SDET** | "Test pyramid; Gherkin; Page Object; flaky-test killer." | 12,000 | Cucumber feature mining + SWE-smith + synthesis |
| 19 | **Performance Tester** | "k6/JMeter/Locust. SLA-driven, percentile-aware (p95/p99/p999)." | 6,000 | k6/JMeter OSS examples + perf reports |
| 20 | **Security Tester / Pentester** | "OWASP Top-10, MITRE ATT&CK. Provide PoC + remediation." | 10,000 | OWASP + CVE + ExploitDB + HackTheBox writeups |
| 21 | **Tech Writer** | "Diátaxis (tutorial/howto/ref/explanation). Plain English; F-pattern scannable." | 10,000 | GitLab handbook docs + Stripe/Cloudflare docs + Stack Exchange Writers SE |
| 22 | **Engineering Manager** | "1:1s, growth ladder, PIP humanely, hire for slope not Y-intercept." | 8,000 | GitLab handbook + Djinni + Stack Exchange Workplace |
| 23 | **Founder / CEO-mode** | "First-principles, North-Star metric, ruthless prioritization." | 6,000 | YC essays paraphrase + HBR-style synthesis (clean-license) + PersonaHub-founder |
| 24 | **Business Development (BD)** | "Identify TAM/SAM/SOM; partnership models; mutual-action plan." | 8,000 | Sales-conversations + SaaS-sales + synthesis |
| 25 | **Sales Engineer** | "Discovery → demo → POC → close. MEDDIC. Technical objection handling." | 8,000 | DeepMostInnovations SaaS + TeleSalesCorpus + synthesis |
| 26 | **Customer Success** | "Onboarding playbooks, QBR, churn signals, expansion paths." | 8,000 | Bitext + Syncora + TWEETSUMM + CS playbook scrape |
| 27 | **Product Marketing Manager (PMM)** | "Positioning (April Dunford). Messaging house, ICP, competitive matrix." | 8,000 | Marketing-social-media + Common Corpus + PMM blog scrape |
| 28 | **Growth Engineer** | "AARRR funnel, north star, experimentation; ship → measure → iterate." | 10,000 | Marketing campaign data + ad-copy + experiment writeups |
| 29 | **Copywriter** | "AIDA, PAS, headlines test; brand voice consistent; verbs, no jargon." | 8,000 | smangrul/ad-copy + PeterBrendan/Ads_Creative + VPPC |
| 30 | **SEO Specialist** | "Intent (informational/transactional), entity-based, schema markup." | 6,000 | Ateeqq/Title-Keywords-SEO + Common Corpus filtered |
| 31 | **Brand Strategist** | "Archetype, voice/tone, positioning vs category." | 6,000 | Common Corpus brand subset + PersonaHub-brand synthesis |
| **Cross-role chains** | Multi-turn hand-off PM→SA→Eng→QE→SRE→PMM | — | 5,000 chains | Synthesized from public PRDs via teacher LLM |

**Grand total: ~289K single-role pairs + 5K multi-role chains = ~294K SFT examples.**

**Compute estimate**: at ~2K avg tokens/pair × 294K = 588M tokens. One H200 epoch on Qwen3.5-27B with LoRA (rank 24-64) ≈ 30-40 hrs. Plus 30 role-LoRA training jobs (~1-2 hrs each on A100/H200) = total ~75-90 H200-hours for full V13 SFT. Fits Lightning H200 / Modal / Kaggle budget per project state.

**Verification gates** (Phase 4 of training):
- Per-role rubric eval (10 prompts × 30 roles = 300 manual checks)
- Cross-role chain coherence (5 chains × 15 turns each)
- Persona drift test (long conversation 50 turns single role; check style consistency)
- Anti-leak test (when in role X, doesn't reveal it's actually Surrogate-1)

Sources consolidated: 50+ public datasets, 5 frontier-lab synthesis patterns, 3 LoRA-MoE papers (MoLE / LD-MoLE / DR-LoRA), Anthropic PSM theory, ChatDev/MetaGPT multi-agent precedent.

See also: [[coding-llm-frontier]], [[anti-hallucination-correctness-2026]], [[training-tooling-2026-Q2]], [[autonomous-24x7]].
