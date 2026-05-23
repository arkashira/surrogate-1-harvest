---
name: qa-run-workflow
description: Standard workflow for executing a QA run on a project, capturing test results, and logging findings for decision tracking.
version: 1.0.0
author: Ashira
---

## Purpose
Automates the end‑to‑end QA process used in the Axentx swarm pipelines:
1. Initialize a QA run via `pipeline-helper.sh` to obtain a unique run ID and project context.
2. Detect the project's primary test framework (npm/vitest, pytest, etc.).
3. Execute the tests, handling timeouts and environment flags.
4. Record the outcome (pass/fail) and any error messages.
5. Persist a markdown decision file under `~/.hermes/workspace/swarm-shared/decisions/` with a standard schema.
6. (Optional) Query the knowledge graph for relevant tags to surface prior lessons.

## Steps
1. **Run helper**
   ```bash
   bash ~/.hermes/scripts/pipeline-helper.sh qa
   ```
   Capture `RUN_ID`, `PROJECT`, and `PROJECT_PATH` from the output.
2. **Determine test command**
   - If `package.json` contains a `test` script → `npm test --silent` in the UI directory.
   - If a `pytest` configuration exists (`pyproject.toml` or `setup.cfg`) → `pytest -q` at the project root.
   - Adjust environment variables as needed, e.g. `NOTIFICATIONS_RESOURCE_TEST_NO_DOCKER=true` for Arkship.
3. **Execute tests**
   Use the `terminal` tool with a reasonable timeout (e.g., 300 s). Capture stdout, stderr, and exit code.
4. **Write decision file**
   ```json
   {
     "dev_run_id": "<RUN_ID>",
     "passed": <true|false>,
     "findings": ["<human‑readable error or success messages>"]
   }
   ```
   Save to:
   `~/.hermes/workspace/swarm-shared/decisions/<RUN_ID>_qa.md`.
5. **(Optional) Knowledge Graph lookup**
   ```bash
   ~/.claude/bin/graph-query.sh tag devops
   ```
   Use the output to add any relevant historical patterns to the decision record.

## Pitfalls & Tips
- Some Arkship tests depend on Docker; set `NOTIFICATIONS_RESOURCE_TEST_NO_DOCKER=true` to run them locally.
- Ensure the working directory matches the location of the test runner (UI sub‑dir for npm, repo root for pytest).
- If no test files are found, record a finding like "No test files found, exiting with code 1".
- Always write the decision file even on failure; downstream reviewers depend on it.

## Example Decision File
```json
{
  "dev_run_id": "20260422_0902_arkship_qa",
  "passed": false,
  "findings": [
    "Tests failed to run: ModuleNotFoundError: No module named 'index'",
    "Pytest exited with error code 3",
    "Set NOTIFICATIONS_RESOURCE_TEST_NO_DOCKER=true or fix test environment"
  ]
}
```

## Tags
- qa
- testing
- automation
- arkship
---