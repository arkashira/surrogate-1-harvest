# Runbook: Agent stuck in retry loop

Alert code: `agent_loop_storm`
Severity: warn → critical if a single agent burns > 10k LLM tokens/h
on a single item.

## Symptoms

- One queue (typically `review-queue` or `dev-queue`) has the same
  `id` repeatedly appearing in `axentx-{role}-daemon.log` for > 1
  hour. Look for `▸ <id>` lines repeating without progressing.
- Token-budget tracker (`bin/budget-tracker.sh`) shows the role spiked
  vs its rolling baseline — for example reviewer normally does 3-5k
  tokens per item, but is suddenly logging 30-50k on a single id.
- Discord starts receiving repeated "reviewer rejected" messages on
  the same item id.
- The agent's history array on the queue file (`state/swarm-shared/<queue>/<id>.json`) has > 5 dev attempts and > 5 review attempts — way past the 3-attempt cap.

## Immediate action (in order)

1. **Identify the stuck item**: `ls -lt state/swarm-shared/review-queue/ | head -10` — the file modified most recently, repeatedly, is your culprit.
2. **Inspect**: `jq '. | {id, project, focus, history_len: (.history|length), last_actor: .history[-1].actor, last_at: .history[-1].at}' state/swarm-shared/review-queue/<file>.json`.
3. **Move it out of the loop**: `mv state/swarm-shared/review-queue/<file>.json state/swarm-shared/done-stuck/`. Create the dir if it doesn't exist. The agent immediately moves to the next item.
4. **Confirm the storm stops**: `journalctl -u axentx-reviewer-daemon -n 20 -f` — should show the agent picking a different item within the poll interval.
5. **Tag the stuck item** for offline analysis: `jq '. + {stuck_marker: "moved at '"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}' state/swarm-shared/done-stuck/<file>.json > /tmp/_; mv /tmp/_ state/swarm-shared/done-stuck/<file>.json`.

## Root cause — common patterns

- **Reviewer rubric impossible to satisfy**: dev produces correct code,
  reviewer asks for an additional test, dev adds the test, reviewer
  asks for a different test, ad infinitum. Fix: 3-attempt cap should
  prevent this — verify it is enforced. The cap lives in
  `axentx-reviewer-daemon.py`, look for `MAX_ATTEMPTS = 3`.
- **JSON parse error in the loop**: dev returns malformed JSON, reviewer
  fails to parse, marks "reject", dev resubmits same malformed JSON.
  Fix: validate JSON in `axentx_pipeline.synthesize` before passing to
  reviewer; reject and re-roll within the dev daemon.
- **Conflicting rubrics**: a recent rubric update introduced a
  contradiction (e.g. "no comments" + "comment every public function").
  Fix: roll back the rubric change, log a postmortem, add a CI check
  that enforces rubric consistency (regex against opposing keywords).
- **LLM provider giving deterministic bad output**: temperature=0 and
  the same prompt keeps producing the same wrong response. Fix: bump
  temperature to 0.3 in the affected role, or switch the next provider
  in the chain.

## Post-incident

- File a tag in `state/training-shards/dpo.jsonl` for the stuck item if
  it had > 5 alternations (great DPO signal — model learned something
  the model itself can't fix without intervention).
- Add the symptom + fix to this runbook.
- If the stuck loop was caused by a rubric update, write a regression
  test that re-runs the stuck item against the previous + new rubric
  and ensures progress is made.

## Escalation

- Same item id stuck again after re-queue → it is not a transient,
  pull the entire item out of the pipeline and treat it as a failed
  task; do not auto-retry.
- Multiple items stuck across multiple agents simultaneously → likely
  a downstream service issue (LLM provider returning garbage). Check
  `docs/runbooks/cf-worker-down.md` and the LLM chain status.

## Related

- `docs/runbooks/cf-worker-down.md`
- `bin/axentx-reviewer-daemon.py` (3-attempt cap).
- `bin/budget-tracker.sh` (token spike detection).
