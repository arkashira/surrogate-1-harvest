# Surrogate-1 v2 — capability roadmap

> "ทุกสิ่งทุกอย่างที่ให้ทำทั้งหมด เป็นฟีเจอร์ของ model surrogate-1 ทั้งหมด
>  ถึงจะ research เอามาใช้ตอนนี้ แต่สุดท้าย เอาไปใส่ให้ surrogate-1 ทำงาน
>  พวกนี้ได้ทั้งหมด ในระดับ model เลย" — ฟิวส์, 2026-05-02

This document is **the source of truth for what Surrogate-1 v2+ must do
natively** — not as orchestration around a foundation model, but as
fine-tuned weights inside a single coherent agent. Every daemon we ship
in the v1 era is also a **capability target for v2 training**.

The training corpus that feeds v2:

1. `state/training-pairs.jsonl` — SFT/DPO/verdict harvested by
   `agent-decisions-to-pairs.py`
2. `state/swarm-shared/decisions/` — per-stage decisions (synced to git)
3. `state/swarm-shared/done/` — full pipeline traces (every successful
   research → bd → … → commit chain)
4. `state/skills/` — auto-synthesized SKILL.md files (NEW)
5. `state/papers/` — auto-written technique papers (NEW)
6. Public corpora: HF datasets we harvest (StackOverflow / GitHub Issues
   / Reddit OAuth-pulled / dev.to / Lobsters / ProductHunt)

Every capability below has a corresponding **dataset slice** in the
training pairs; trainer V20+ already picks them up by `flavor` tag.

---

## Capability matrix — what v2 must do natively

| # | Capability | v1 implementation (now) | v2 weight target | Training signal |
|---|------------|-------------------------|------------------|-----------------|
| 1 | **Read the room** — adapt tone per user | `hermes-discord-bot.py` SYSTEM_PROMPT + per-user profile injection | dialog-style mixing casual / playful / engineer / formal driven by name+history+interest tags | `chat_history.jsonl` (1 dataset row per turn-pair, conditioned on profile JSON) |
| 2 | **Per-user memory** — never forget context, recommendation-flavored | `state/chat-memory/profiles.json` + `history/{user_id}.jsonl` | retrieval-conditioned generation: profile + last 20 turns in prompt | profile + 20 turns + assistant reply, masked across N users |
| 3 | **Pain extraction** from arbitrary post | research-daemon → `RESEARCH_SYSTEM` JSON output | `extract_pain(post) → {is_real, severity, audience, evidence}` head | every research-stage `history` entry → SFT pair |
| 4 | **Pain validation** (cross-source) | pain-validator-daemon (new this turn) | `validate_pain(pain, neighbors[]) → {confirmed, dupe, refined}` | (post, neighbors, validator-verdict) triples |
| 5 | **BD triage** — EXTEND / NEW-PRODUCT / PASS | bd-daemon + `call_llm_strong()` | classification head with chain-of-thought, hard PASS on anti-patterns | bd-stage history → SFT, plus DPO pairs (PASS vs EXTEND for same pain) |
| 6 | **Design thinking** — root cause + JTBD | design-thinking-daemon | structured "5-why → JTBD → user-archetype → constraints" prompt template | design-stage history → SFT |
| 7 | **Business model canvas** — BMC + pricing + NSM | business-daemon | strict-JSON BMC schema with grounding citations | business-stage history → SFT |
| 8 | **GTM positioning** — Geoffrey Moore template | marketing-daemon | positioning + ICP + competitor map + first-7-days plan | marketing-stage history → SFT |
| 9 | **PRD synthesis** — epics + stories + tasks | prd-daemon | strict-JSON PRD with files-hint + acceptance criteria | prd-stage history → SFT |
| 10 | **Code synthesis** — diff + verification | dev-daemon `synthesize()` | code-completion head with rubric-aware refinement | dev-stage SFT + DPO (chosen=approved, rejected=initial) + verdict triples (rejected_dev + reviewer_feedback + refined_dev) |
| 11 | **Code review** — reviewer rubric | reviewer-daemon | structured reject reasons with per-blocker citation | reviewer-stage history; the verdict-triple flavor |
| 12 | **QA / test gen** — derive tests from acceptance | qa-daemon | test-first generation given story + acceptance | qa-stage history → SFT |
| 13 | **Commit + push** — repo-aware path picking | commit-daemon | repo-conditioned generation respecting CODEOWNERS / structure | commit-stage history (file-paths chosen) |
| 14 | **Release versioning** — semver bump + notes | release-daemon (24h) | classify {major/minor/patch} from commit-subject log + notes | (commit-log, bump, notes) triples; daily |
| 15 | **Self-heal** — restart-or-fix decisions | oci-self-heal-daemon | "given systemd state + log tail, fix-attempt" | (svc-name, journalctl-tail, action) triples |
| 16 | **Incident response** — auto-rerun GH/Render | axentx-incident-responder-daemon | classify failure pattern → action map | failure-action pairs from state file |
| 17 | **Skill synthesis** (NEW) — when failure repeats, write a SKILL.md | axentx-skill-synthesizer-daemon (this turn) | meta-capability: `synthesize_skill(repeated_failure) → SKILL.md + verifier` | (problem-pattern, skill-content, verification-result) triples |
| 18 | **Paper writing** (NEW) — when a synth is mature, write a publishable note | axentx-paper-writer-daemon (this turn) | structured technical-note generation with citations | (skill, validation-trace, paper) triples |
| 19 | **Recommendation per user** — what topic / project to surface | per-user profile + topic interests | retrieval-grounded ranking | (user-profile, candidate-topics, picked-topic) triples |
| 20 | **Browser/scraper bypass** — OAuth/API/rate-limit handling | research-daemon Reddit OAuth + retry-after honoring | `pick_strategy(target) → {oauth|api|rss|playwright|archive}` | (target-host, strategies-tried, success-strategy) triples |

---

## Training data harvest — what's wired now

| Stage | Pairs file | Status |
|---|---|---|
| dev / review / qa / commit | `state/training-pairs.jsonl` (SFT, DPO, verdict) | live since 2026-05-01 |
| research / bd / design / business / marketing / prd | (extending in this turn) | NEW |
| chat memory (Discord) | `state/chat-memory/history/{user_id}.jsonl` | live since 2026-05-02 |
| self-heal + incident | `state/self-heal/*.attempts` (decision logs) | live |
| skill synth | `state/skills/*.md` (with metadata frontmatter) | NEW |
| papers | `state/papers/*.md` | NEW |

All synced every 5 min to `state` orphan branch on
`arkashira/surrogate-1-harvest` so a fresh VM (or v2 trainer) can pull
the full corpus with one `git clone -b state`.

---

## How v2 training picks this up

1. `surrogate-1-train-v18-mission.py` already concatenates several HF
   datasets. Add new mounts:
   - `axentx/surrogate-1-decisions` (decisions/*.md)
   - `axentx/surrogate-1-skills` (skills/*.md)
   - `axentx/surrogate-1-papers` (papers/*.md)
   - `axentx/surrogate-1-chat-memory` (privacy-scrubbed chat-history)

2. `agent-decisions-to-pairs.py` already pushes SFT/DPO/verdict to
   `axentx/surrogate-1-training-pairs`. Extending it (this turn) to
   cover all 6 discovery stages multiplies signal density ~7x.

3. Trainer applies the **flavor weight curriculum**:
   - SFT first (broad capability)
   - DPO second (preference / rejection learning)
   - Verdict-triple third (read-and-address-feedback)
   - Skill-grounded (this turn) — model learns to USE its own synthesized skills

---

## Surrogate-1 v1 → v2 rollout policy

- v1 STAYS OUT of the LLM fallback chain until **measured** to beat the
  weakest provider in the chain on our internal eval (`tests/llm-eval.py`,
  to be added).
- v2 ROLLS IN as a NEW provider in the chain at position 6-8 (mid-chain),
  not as last-resort fallback. Last-resort fallback selection biases
  evaluations toward weaker outputs — opposite of what we want.
- v3+ promotes only by winning head-to-head on real pipeline cycles
  (DPO label = "surrogate-N output preferred over chain"). Eval data
  comes from the verdict triples stored in training-pairs.

---

## Open capabilities (target for v3+)

- **Multi-step planning with rollback** (current dev daemon is single-shot
  with N candidates — v3 should plan + execute + verify + rollback as one
  unit)
- **Tool use** at inference time (function calling against the same APIs
  the daemons use today: write_item, advance, gh, sb_request)
- **Cross-conversation continuity** for a single user (DM threads + #channels
  unified into one session-aware context window)
- **Multi-modal** — read PR diffs as code AST (not just text) and Discord
  screenshots as UI states, not OCR-prose

---

Updated: 2026-05-02 by axentx self-improvement loop.
