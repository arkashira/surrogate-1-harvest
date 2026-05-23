#!/usr/bin/env bash
# DynaDebate Verification Agent — fires when tournament review produces tied/disagreeing
# verdicts. Spawns a Sonnet agent with Bash + WebFetch permissions to INDEPENDENTLY verify
# the claim (not re-vote, but actually run code/search docs/check facts).
# Result: tie-breaker backed by real-world evidence.
set -u

LOG="$HOME/.claude/logs/dynadebate.log"
TOURNAMENT_DIR="$HOME/.hermes/workspace/tournaments"
VERIFIED_DIR="$HOME/.hermes/workspace/dynadebate-verified"
mkdir -p "$(dirname "$LOG")" "$VERIFIED_DIR"

# Find recent tournaments where verdict is ambiguous (scores within 1 of each other)
/usr/bin/python3 <<'PYEOF' >> "$LOG"
import json, os, re, subprocess, time
from pathlib import Path

TOURN = Path.home() / '.hermes/workspace/tournaments'
VERIFIED = Path.home() / '.hermes/workspace/dynadebate-verified'
STATE = Path.home() / '.claude/state/dynadebate-seen.txt'
STATE.parent.mkdir(parents=True, exist_ok=True)
STATE.touch()

seen = set(STATE.read_text().splitlines())
cutoff = time.time() - 3600

debates = 0
for tf in TOURN.glob("*_tournament.json"):
    if tf.stat().st_mtime < cutoff: continue
    if tf.name in seen: continue
    try: v = json.load(open(tf))
    except: continue

    # Check if tournament was "close" — ranking scores within 1 of each other
    ranking = v.get('ranking', [])
    if len(ranking) < 2: continue
    scores = sorted([r.get('score', 0) for r in ranking], reverse=True)
    gap = scores[0] - scores[1] if len(scores) >= 2 else 99

    is_close = gap <= 1  # tied or near-tied
    has_blockers = bool(v.get('blockers'))
    needs_verification = is_close or has_blockers

    if not needs_verification: continue

    prio = v.get('prio', 'unknown')
    candidates_paths = v.get('candidates', [])[:3]  # top 3 to verify

    # Build verification prompt — use Sonnet with tools
    candidate_code = []
    for p in candidates_paths:
        pth = Path(p)
        if not pth.exists(): continue
        body = pth.read_text()[:2500]
        # Extract first code block
        m = re.search(r'```(\w+)\n(.*?)```', body, re.DOTALL)
        if m:
            candidate_code.append(f"--- {pth.parent.name} ---\n```{m.group(1)}\n{m.group(2)[:1500]}\n```")

    if len(candidate_code) < 2: continue

    prompt = f"""You are the DynaDebate Verification Agent. The tournament voting was CLOSE (gap={gap}) for priority '{prio}'. Your job: INDEPENDENTLY verify by running/checking, not voting.

Available tools: WebFetch (check library docs), WebSearch (check API existence).

Tasks:
1. For each candidate, identify ONE specific verifiable claim (e.g., "uses boto3.client('ce') which accepts Filter={{'Dimensions':{{'Key':'LINKED_ACCOUNT'}}}}" — is that real?)
2. For each claim, verify via WebFetch on the official docs URL
3. Report which candidate has the FEWEST hallucinated API calls

{chr(10).join(candidate_code)}

Output STRICT JSON:
{{
  "verified_claims": [{{"candidate": "...", "claim": "...", "verdict": "real" | "hallucinated" | "partial", "evidence_url": "..."}}],
  "winner_after_verification": "<candidate name>",
  "confidence": 0.0-1.0,
  "hallucination_count_per_candidate": {{"name1": N, "name2": N}}
}}"""

    # Fire Sonnet (with tools enabled — claude-bridge allows WebFetch/WebSearch)
    try:
        r = subprocess.run(
            ["/opt/surrogate-1-harvest/bin/claude-bridge.sh", "--model", "sonnet", "--timeout", "300"],
            input=prompt, capture_output=True, text=True, timeout=360
        )
        if r.returncode != 0 or not r.stdout: continue

        # Save
        result_file = VERIFIED / f"{prio}_verified_{int(time.time())}.json"
        result_file.write_text(r.stdout)
        with open(STATE, 'a') as f: f.write(tf.name + '\n')
        debates += 1
        print(f"✅ {prio}: verified (gap was {gap})")
    except Exception as e:
        print(f"⚠️ {prio}: error {e}")

    if debates >= 2: break  # cap per run (Sonnet cost)

print(f"total debates resolved: {debates}")
PYEOF
