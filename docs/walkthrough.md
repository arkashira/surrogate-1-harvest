# A day in the life of surrogate-1

## 06:00 UTC — quiet hours
- 30 daemons idle on GCP e2-micro
- watchdog sweeps every 5 min: pings 6 HF Spaces + Cloudflare cron does the same independently
- self-heal-daemon polls dead services; nothing to do
- training: Kaggle V19 #N may be running (12-hr wall accountant)

## 09:00 UTC — discovery wakes
- `axentx-research-daemon@1` polls HN, dev.to, Lobsters
- LLM filters posts; one survives sev≥5 → `bd-queue`
- `axentx-trends-daemon` (every 6h) hits ProductHunt + GitHub trending

## 09:05 UTC — triage cascade
- bd daemon picks up the pain (60s poll); verdict: EXTEND `Costinel`
- design daemon runs 5-whys + JTBD validation; PROCEED
- business daemon builds BMC + market sizing; verdict: BUILD
- marketing daemon drafts positioning + GTM
- ux daemon adds user flows + wireframes
- prd daemon decomposes into 4 epics × 3-5 stories × 1-3 tasks each
- ~20 dev tasks land in `dev-queue`

## 09:25 UTC — engineering picks up
- `axentx-dev-daemon@1..6` start consuming dev-queue
- Each task runs through:
  - dev (synthesize from 3 LLM candidates) → review-queue
  - reviewer (rubric v1) → qa-queue (or back to dev with feedback ↺ ≤3 attempts)
  - qa (TDD plan) → security + perf parallel verdicts
  - commit daemon writes `.axentx-dev-bot/<id>.md` into the project repo
  - git add + git commit + git push (with auto-rebase if conflict)

## 09:40 UTC — first commits ship
- 5+ auto-bot commits land on `axentx/Costinel` main
- docs daemon scans them; if user-facing surface changed, drafts README+CHANGELOG diff
- content daemon scans every 4h; if ≥3 commits in window, drafts blog post

## 10:00 UTC — training pipeline catches up
- `agent-decisions-to-pairs` (every 15 min) extracts (SFT, DPO, verdict) records
- `push-training-to-hf` (every 5 min) ships chunks of `training-pairs.jsonl` to `axentx/surrogate-1-training-pairs`
- `rag-build` (every 30 min) re-indexes new decisions into Vectorize

## 12:00 UTC — release rhythm
- release daemon (every 24h) checks each repo: ≥5 commits since last tag → semver bump → `gh release create`

## 18:00 UTC — researcher catches new pain
- another HN post; cascade fires again
- bd verdict this time: NEW-PRODUCT
- architect daemon picks it up: drafts ADR + tech stack + folder layout
- prd daemon emits scaffold-aware tasks → dev-queue
- new product gets bootstrapped into a fresh GitHub repo (manual today, automatable later)

## 21:00 UTC — quiet again
- daemons idle; pipeline backlog drains overnight
- any 3-attempt force-approves get tagged `needs_iteration` for tomorrow's retro

## Costs in a typical day
| | Free quota | Today | %used |
|---|---:|---:|---:|
| GCP e2-micro | 24h × 1 vCPU | 24h | 100% (always on) |
| CF Workers | 100k req/day | ~2k | 2% |
| CF D1 | 5M reads / 100k writes | ~5k / 200 | <1% |
| CF Workers AI | 10k neurons/day | ~2k | 20% |
| CF Vectorize | 5M dim/mo | 1.4M static | 28% |
| Supabase | 500MB / 2GB egress | 50MB / 100MB | 5-10% |
| HF Hub | unlimited public | 50MB push/day | n/a |
| LLM providers (combined) | varies | ~500 calls | varies |

Total spend: $0/month. ✅
