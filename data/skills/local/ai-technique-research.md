---
name: ai-technique-research
description: Discover recent AI/ML techniques via arXiv API and generate a proposal for a specific project.
version: 1.0.0
author: Hermes Team
license: MIT
metadata:
  hermes:
    tags: [research, arxiv, technique, proposal]
---

# AI Technique Research Skill

## Purpose
Automatically find a cutting‑edge AI/ML technique published in the current year, evaluate its fit for a given project, and create a concise proposal document. Also records the decision in the backlog and (optionally) adds a memory entry.

## Prerequisites
- Bash environment with `curl`, `grep`, `jq`.
- Access to the arXiv API (`https://export.arxiv.org/api/query`).
- Project workspace containing `decisions/` and `backlog.jsonl` as per the standard Hermès swarm layout.

## Steps
1. **Obtain RUN context** (optional but useful for logging):
   ```bash
   bash ~/.hermes/scripts/pipeline-helper.sh ai-rd
   ```
   Capture `RUN_ID` and `PROJECT` from the output.
2. **Search arXiv for recent techniques**:
   ```bash
   YEAR=$(date +%Y)
   QUERY="all:${YEAR}+AND+all:technique"
   curl -s "https://export.arxiv.org/api/query?search_query=${QUERY}&max_results=20" \
     | grep -i "<title>" | head -n 5
   ```
   Review the titles manually or add additional filters (e.g., `resource allocation`, `reinforcement`).
3. **Select a technique** – choose the most relevant title.
4. **Write proposal markdown** in the project decisions folder:
   ```bash
   cat <<EOF > ${PROJECT_PATH}/decisions/${RUN_ID}_ai-rd.md
   Technique: <Chosen Technique>
   Fit: <Brief justification for the project>
   Effort: <Low|Medium|High> (estimate)
   EOF
   ```
5. **Append to backlog** (optional):
   ```bash
   jq -n --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
         --arg source "self-improvement" \
         --arg project "${PROJECT}" \
         --arg item "${Chosen Technique}" \
         '{ts:$ts, source:$source, project:$project, item:$item}' \
         >> ${SHARED_BACKLOG}
   ```
6. **Record memory** (if the environment supports it):
   ```bash
   memory add "${PROJECT}/${ROLE}: evaluated ${Chosen Technique} for ${PROJECT}" target=memory
   ```
   (Skip if `memory` tool is unavailable.)

## Tips
- Refine the arXiv query with additional keywords to narrow to your domain (e.g., `resource allocation`, `zero trust`).
- Use `sortBy=submittedDate&sortOrder=descending` to get the newest papers first.
- If the technique is very new and lacks public implementations, mark effort as **High**.
- Keep the proposal concise (one‑sentence fit, one‑sentence effort).

## Example Output
```
Technique: Reward Engineering for Reinforcement Learning in Software Tasks
Fit: Can be applied to optimize resource allocation and task scheduling in Arkship's cloud orchestration layer, improving efficiency and cost.
Effort: Medium (prototype in 2 weeks, evaluation and integration 4 weeks total).
```