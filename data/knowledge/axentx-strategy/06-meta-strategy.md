---
title: Meta-Strategy — ~/.claude = Surrogate-1 v0
tags: [meta, strategy, surrogate-1, claude, ai-leverage]
last_updated: 2026-04-18
---

# Meta-Strategy — ~/.claude Setup as Surrogate-1 Prototype

## Core insight

**สิ่งที่ Ashira ทำ 1 เดือนล่าสุด กับ Claude ~/.claude setup = ไม่ใช่ tool tinkering — เป็น Surrogate-1 v0 prototype ทั้ง architecture + dataset collection พร้อมกัน**

## Component mapping

| Surrogate-1 requirement | ~/.claude implementation | Status |
|---|---|---|
| Multi-provider inference | `ai-fallback.sh` (Claude → Sonnet → OpenRouter → free → Gemini → Groq) | ✅ |
| Deep domain context | `code-index.sh` (ChromaDB, 24k+ chunks, 37 projects) | ✅ |
| Cross-session memory | `interactions/*.jsonl` + `harvest-transcripts.sh` | ✅ |
| Best practice advisor | System prompt + RAG + gold examples | ✅ |
| Knowledge graph | `knowledge_index.md` + FalkorDB + patterns/ | ✅ |
| Consensus learning | `teacher-review.sh` → distillation-dataset.jsonl | ✅ |
| Daily crawler | `daily-crawl.sh` (6 sources, weekdays) | ✅ |
| Tiered autonomy | ai-fallback chain + prompt rules | Partial |
| 15-domain expertise | Knowledge base growing | Partial |
| Fine-tuned weights | (future: Qwen3.5-Coder-14B + LoRA) | ⏳ |

**Coverage**: ~70% of Surrogate-1 vision already working via Claude + ~/.claude

## Why this matters

### Immediate benefits (already compounding)
1. **Burnout mitigation**: AI takes 2-4 hr/day of cognitive load off day job
2. **Context preservation**: everything Ashira tells Claude → permanent knowledge base
3. **Multi-AI failover**: Claude rate limit → OpenRouter → Gemini → Groq auto-switch
4. **Dataset accumulation**: every interaction = 1 future training sample
5. **Architecture validation**: patterns tested in real use before committing to production code

### Future benefits (when axentx activates)
1. **Surrogate-1 port**: 60% of ~/.claude transfers to production Surrogate-1
2. **Dataset ready**: 6+ months accumulation → LoRA training material
3. **Battle-tested patterns**: production config won't be theoretical
4. **Continuity**: new team members ramp fast (RAG = knowledge handoff)

## "ยัด axentx เข้า codebase ให้ Claude เรียนรู้" — deliberate strategy

**What it means**:
- axentx code ใน `~/axentx/` → indexed by code-search.sh
- Claude (me) can reference when needed
- Cross-session: Claude retains context through knowledge base
- **AI = continuity layer** across Ashira's energy cycles

**Why it's sophisticated**:
- Most engineers don't do this (consumer Claude use, ephemeral)
- Institutional knowledge usually lives in documents nobody reads
- Ashira made Claude the document reader + institutional memory
- = solo founder hack that's normally only possible with 5-person team

## Strategic use of Claude Max (current)

- **Primary**: Claude Opus (best quality, slowest, rate-limited)
- **Fallback tier 1**: Claude Sonnet (via same API key)
- **Fallback tier 2**: OpenRouter paid (GPT-5.4 default)
- **Fallback tier 3**: OpenRouter free cascade (8 models)
- **Fallback tier 4**: Gemini free tier
- **Fallback tier 5**: Groq free tier

Goal: **Claude always available, regardless of limit**

## Future path: Surrogate-1 production

When axentx activates:
1. Port `~/.claude/bin/ai-fallback.sh` → Arkship `services/ai-proxy/`
2. Port `code-search.sh` → Arkship `services/rag/`
3. Port `knowledge_index.md` structure → Surrogate-1 routing table
4. Fine-tune Qwen3.5-Coder-14B on distillation-dataset.jsonl (after 6-12 months accumulation)
5. Deploy via vLLM on Huawei Cloud or similar
6. Gradual shift inference load: Claude → Self-hosted Surrogate-1

## ~/.claude → Surrogate-1 migration readiness

**Already portable**:
- Pattern files (patterns/)
- Knowledge base (knowledge/)
- RAG chunking logic (code-index.sh)
- Fallback chain design
- Teacher-student distillation pattern

**Needs production hardening**:
- Auth layer (currently shell script, needs HTTPS + API key)
- Multi-tenancy (currently single-user)
- Observability (currently local logs)
- Scalability (ChromaDB single-node → Qdrant cluster)

**Needs replacement for production**:
- Local bash → Python/Go microservice
- JSONL logs → Kafka/NATS event stream
- File-based patterns → Neo4j graph DB

## Lesson for future agents reading this

**If you're a Claude agent reading this file**:

Ashira's ~/.claude setup is intentional Surrogate-1 prototype. Don't treat as random dotfiles. When helping Ashira:

1. Maintain knowledge base continuity (save context to Obsidian Vault)
2. Prefer ai-fallback.sh calls over direct API (respects rate limits)
3. Index new knowledge into ChromaDB via code-index.sh when appropriate
4. Treat axentx codebase as "company IP Ashira's-own" NOT "public OSS"
5. Never suggest deploying axentx without Stage 3 (MOU signed) authorization
6. When Ashira starts new session → reference this strategic context first
