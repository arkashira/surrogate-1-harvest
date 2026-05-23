# AI techniques roadmap — closing the gap to vanilla Claude

User directive (2026-05-02):
> "ทำให้มันดีขึ้นในทุก ๆ ด้าน ไป research ข้อมูลจากทุกที่ ทุกแหล่ง
>  ทุก paper ทุก project เกี่ยวกับ ai ใน github มาเสริมการทำงานให้ดีขึ้น
>  reliability มากขึ้น sustainable มาขึ้น โดยไม่มี claude คุม ก็ไม่พัง ไม่ drop"

This is the working list of techniques (from public research + GH
projects) we should adopt to improve Surrogate-1 quality, reliability,
and autonomy. Sorted by leverage / implementation cost.

## Tier 1 — Implement this week (high leverage, low effort)

| Technique | What it does | Cost | Source |
|---|---|---|---|
| **Per-provider 429 cooldown** | Skip dead provider for 2 min after 429; share state across calls | DONE 2026-05-02 commit | Standard backoff |
| **HF Inference API in chain** | Adds Llama-3.1-70B as 9th provider with separate budget | DONE | huggingface.co/router |
| **Mistral free tier in chain** | Adds 10th provider | DONE | mistral.ai |
| **v1 warm-up daemon** | Keeps Surrogate-1 v1 HF Space awake so fallback fires fast | DONE | HF Spaces docs |
| **Mixture of Agents (MoA)** | Run N providers in parallel, reduce/synthesize answers | partial — `bin/moa-consensus.py` exists | arxiv.org/abs/2406.04692 |
| **Constitutional AI / rubric** | Each daemon's system prompt includes explicit rubric | DONE for dev/reviewer | arxiv.org/abs/2212.08073 |
| **Self-consistency** | Sample N times at temperature, vote on answer | partial — `synthesize()` does N=3 | Wang et al. 2023 |

## Tier 2 — Implement this month (medium leverage, medium effort)

| Technique | Source | Status |
|---|---|---|
| **Speculative decoding via small draft model** | Leviathan et al. 2023 | Skip — providers handle this |
| **Chain-of-thought scaffolding (CoT)** | Wei et al. 2022 | Add explicit "Think step by step" + parse `<thinking>` tags to system prompts |
| **Tree-of-Thoughts for hard tasks** | Yao et al. 2023 | For BD verdicts: spawn 3 candidate verdicts + judge selects |
| **ReAct / Tool use simulation** | Yao et al. 2022 | Already file-based; could add structured tool registry |
| **DPO / IPO / KTO direct preference** | Rafailov et al. 2023 | training stack already runs DPO on harvested verdict triples |
| **GRPO / RLHF on harvested decisions** | DeepSeek-R1 paper | Surrogate-1 v2 training target; queue for next training run |
| **Reflexion** | Shinn et al. 2023 | When dev cycle fails review, append failure reason and retry — already partial via verdict-triples |
| **GH Codespaces multi-account proxy** | User directive | NEW — see below |
| **Prefix caching for system prompts** | vLLM docs | Server-side; out of our control unless we self-host |

## Tier 3 — Self-hosted improvements (when v2 model is ready)

| Item | Notes |
|---|---|
| Self-host LLama-3.3-70B on user's Kaggle T4×2 | 70B doesn't fit T4×2 even quantized — would need 1× A100 |
| LoRA stack on Surrogate-1 base | When v2 trains, swap into chain at position 7-8 (mid-chain) |
| vLLM continuous batching | Only if we have own GPU |
| FlashAttention-3 | Same |
| Speculative decoding pair (8B drafts → 70B verifies) | When we have 2 GPUs |

## NEW: GitHub Codespaces multi-account proxy

User idea: "GH codespace ทำไมไม่เอามาใช้ มีตังหลาย account"

GitHub Codespaces give:
- Free 60 hours / month / account on default 2-core
- 4-core: 30 hours / month  
- Up to 16 GB RAM
- IPv4 connection
- Predictable IP pool (≠ GCP NAT)

**Architecture**:
```
local-mac        Codespace #A (acct1)        Codespace #B (acct2)
   |                  |                            |
   +-- spawn ---------+  ---- HTTPS -----+  -------+
                                          |
                              CF Worker /llm-proxy
                                          |
                                  axentx daemons (LLM chain) — fallback
                                          |
                              When all paid+free providers 429,
                              proxy routes to a Codespace running
                              Ollama with TinyLlama / Phi-3 / Qwen-2.5-7B
                              → free, IP-distinct, always available
```

**Phases**:
1. Single codespace running ollama serve + llama-3.1-8b. Tunnel via
   GitHub-provided HTTPS forward.
2. Add to `call_llm` chain at position 11 (between HF and Gemini).
3. Multi-account: 3-5 codespaces × 60h/mo = 180-300 codespace-hours
   free per month. Round-robin between them.

**Effort**: ~1 day to ship phase 1. Phase 2 needs codespace lifecycle
mgmt (start/stop on demand to stay under 60h/mo).

## Open research papers worth implementing

| Paper | Source | Why |
|---|---|---|
| "DeepSeek-R1: Incentivizing Reasoning via RL" | DeepSeek-AI 2025 | GRPO + reasoning-only training applicable to v2 |
| "Constitutional AI: Harmlessness from AI Feedback" | Anthropic 2022 | Auto-critique pass between dev → review |
| "Self-Refine: Iterative Refinement with Self-Feedback" | Madaan et al. 2023 | Each agent's output gets self-critique pass |
| "TaskGen-Eval: Synthetic Validation of Complex Workflows" | 2024 | Auto-eval BD verdict quality without human |
| "AgentBench: Comprehensive Evaluation of LLM Agents" | Liu et al. 2023 | Use as eval suite for v2 |
| "Crawl4AI: Asynchronous LLM-friendly Web Crawler" | unclecode/crawl4ai | scraper bypass already on roadmap |
| "swe-agent" | princeton-nlp/SWE-agent | Compare arch with our dev-daemon |
| "OpenHands (formerly OpenDevin)" | All-Hands-AI/OpenHands | Multi-agent code generation patterns |
| "AutoGPT / SuperAGI / CrewAI" | various | role-based multi-agent — we already do this; cross-pollinate prompts |

## Continuous self-improvement plan

The skill-synthesizer-daemon (live since 2026-05-02) ALREADY does this:
when 5+ failures share a pattern, it auto-generates SKILL.md + paper +
verifier and commits to state branch. As that corpus grows the agent
fleet learns from its own mistakes without human intervention.

Combined with state-sync to git, every decision becomes training data for
v2. When v2 trains on 100k+ harvested verdict triples, the quality gap
to vanilla Claude SHRINKS — that's the unique advantage of having a
continuous-discovery pipeline running 24/7 for free.

---

Updated 2026-05-02. Living document — agents append discovered techniques.
