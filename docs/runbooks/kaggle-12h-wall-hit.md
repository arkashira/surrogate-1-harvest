# Runbook: Kaggle 12-hour notebook wall hit

Alert code: `kaggle_wall_hit`
Severity: warn — training run is interrupted but resumable.

## Symptoms

- `kaggle-trainer.sh` log shows the notebook went `Stopped` at exactly
  the 12-hour mark from start (give or take 30s).
- The Kaggle notebook page lists status: `Stopped (12h limit)`.
- Training partial checkpoint exists at `~/.surrogate/state/checkpoint-step-N`
  (from `bin/lib/checkpoint.py` — saved every 1000 steps).
- HF Hub model repo for the in-progress adapter has only the most
  recent intermediate weight pushed; full epoch was not completed.

## Immediate action (in order)

1. **Identify last good step**: `ls -la ~/.surrogate/state/checkpoints | tail -5`. The numbered file with the largest `step-N` is your resume point.
2. **Resume**: `bin/kaggle-trainer.sh --resume-from ~/.surrogate/state/checkpoints/step-N --remaining-epochs $(python3 -c 'import json;c=json.load(open("state/training-config.json"));print(max(0, c["epochs"]-c.get("done_epochs",0)))')`.
3. **Wait for new run to land**: Kaggle requires manual notebook trigger via the API. The trainer script handles this; you'll see "kaggle kernels push" output. The run shows up in `https://www.kaggle.com/code/<your-user>/notebook-name`.
4. **Verify checkpoint loads**: the first step in the new run logs `loaded checkpoint from step N` — if it logs `cold start from step 0`, abort immediately (something is wrong with the resume path).

## Root cause

The 12h wall is by design — Kaggle gives free-tier users 30 hours/week
of GPU notebook time, capped at 12h per single run. There is no way
around it on the free tier; we must split training into sub-12h chunks.

Trigger conditions:
- **Epochs too long for the dataset size**: 5 epochs × 50k pairs at
  bs=16 takes ~13h on a P100. Reduce to 4 epochs OR increase batch
  size to 32 (memory permitting).
- **Lost time to setup**: Kaggle notebook spends 6-8 minutes on package
  install and dataset download. If we set 12h budget for *only the
  training step*, we hit the wall during eval. Fix: budget training
  for 11h, leave 1h cushion.
- **Hung evaluation step**: V19 trainer's eval phase sometimes deadlocks
  on a tokenizer race — uses 30+ minutes for what should be 5. Fix:
  evaluate on a fixed 200-prompt held-out set, not the full validation.

## Post-incident

- Update `state/training-config.json` with the actual step count
  achieved + the fact that the run was interrupted. The metadata card
  generator (`bin/write-adapter-card.py`) reads this to mark the
  adapter as "trained over 2 sessions".
- If this is the 3rd interrupted run on the same adapter, switch to a
  smaller epoch budget — the model is probably already converged.
  Eyeball `state/training-loss.csv`: if loss-per-step delta is < 0.001
  for the last 1000 steps, you've already plateaued.
- If the cause is dataset growth (more pairs each week → time grows
  linearly), file an issue to either: (a) sub-sample the dataset to
  fixed 50k pairs/run, or (b) move to Lightning AI Studios where the
  wall is 24h.

## Escalation

- 3 interrupted runs in a row on the same adapter → escalate to
  Lightning AI Studios for a single 24h run. Free tier exists with a
  credit card on file (no charge if under quota).
- If we have a deadline (rare): use Modal $5 free credits for a
  one-shot uninterrupted run on an A10G — costs ~$3 for the same
  workload.

## Related

- `bin/kaggle-trainer.sh` (training entry point).
- `bin/lightning-trainer.sh` (24h alternative).
- `bin/lib/checkpoint.py` (resume logic).
- `state/training-config.json` (epoch + step counts).
