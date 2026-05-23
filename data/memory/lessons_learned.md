## 2026-04-27: GEPA trajectory capture script exec error
- Context: Data-collection script /Users/Ashira/.hermes/scripts/gepa-trajectory-capture.sh failed.
- Insight: The wrapper script was interpreted by Python, causing a SyntaxError at the exec line, likely due to missing shebang or wrong interpreter (cron may default to /bin/sh). This indicates the script was not executed with Bash as intended.
- Fix/Pattern: Ensure the wrapper script has a proper Bash shebang (`#!/usr/bin/env bash`), is executable (`chmod +x`), set `SHELL=/bin/bash` in cron, and invoke the script explicitly via Bash (e.g., `bash /Users/Ashira/.hermes/scripts/gepa-trajectory-capture.sh "$@"`).
- Prevention: Add a pre‑run validation step in cron jobs that checks the shebang line contains "bash" and that the file is executable before execution.
- Tags: #script #cron #bash #gepa #error

## 2026-04-27: budget-auto-throttle script execution failure
- Context: Data-collection script /Users/Ashira/.hermes/scripts/budget-auto-throttle.sh failed.
- Insight: The wrapper script was interpreted by Python due to missing Bash shebang or wrong interpreter, causing a SyntaxError at the exec line.
- Fix/Pattern: Add proper Bash shebang (`#!/usr/bin/env bash`), ensure the script is executable (`chmod +x`), set SHELL=/bin/bash in cron, and invoke explicitly via Bash (`bash /Users/Ashira/.hermes/scripts/budget-auto-throttle.sh "$@"`).
- Prevention: Add a pre‑run validation to verify the shebang contains "bash" and the file is executable before running.
- Tags: #script #cron #bash #budget-auto-throttle #syntax-error

## 2026-04-27: magma-graph-enrich wrapper exec syntax error
- Context: Data-collection script /Users/Ashira/.hermes/scripts/magma-graph-enrich.sh failed during cron execution.
- Insight: The wrapper script was interpreted by Python, causing a SyntaxError at the exec line. This occurs when the script lacks a proper Bash shebang or is invoked with the wrong interpreter.
- Fix/Pattern: Add a Bash shebang (`#!/usr/bin/env bash`), ensure the file is executable (`chmod +x`), and invoke the script explicitly with Bash (e.g., `bash /Users/Ashira/.hermes/scripts/magma-graph-enrich.sh "$@"`). Also set `SHELL=/bin/bash` in the crontab.
- Prevention: Include a pre‑run validation step in cron jobs to verify the shebang contains "bash" and the script is executable before execution.
- Tags: #script #bash #syntax-error #cron #magma-graph

## 2026-04-27: Top hub doc insight
- Context: Ran `knowledge-rag` workflow, identified "MOC" as the central hub.
- Insight: Reviewing this hub early surfaces broad context and prevents missing key knowledge.
- Fix/Pattern: Query hubs and note the top hub.
- Prevention: Make hub review a mandatory pre‑planning step.
- Tags: #knowledge-rag #graph #knowledge-management

## 2026-04-27: Knowledge-RAG pipeline usage
- Context: Ran knowledge-rag workflow after business research script; logged top hub doc insight.
- Insight: Direct redirection to dotfiles is blocked; use safe tools (patch/write_file) for updates.
- Fix/Pattern: Use `write_file` or `patch` instead of `cat >>` for dotfile modifications.
- Prevention: Prefer `patch` for targeted edits; use `write_file` for full replacements.
- Tags: #knowledge-rag #self-improvement #dotfile #security

## 2026-04-27: Top hub doc insight
- Context: Ran knowledge-rag workflow, identified "MOC" as the central hub.
- Insight: Reviewing this hub early surfaces broad context and prevents missing key knowledge.
- Fix/Pattern: Query hubs and note the top hub.
- Prevention: Make hub review a mandatory pre‑planning step.
- Tags: #knowledge-rag #graph #knowledge-management

## 2026-04-27: Opus PR Reviewer script exec syntax error
- Context: Data-collection script /Users/Ashira/.hermes/scripts/opus-pr-reviewer.sh failed during cron execution.
- Insight: The wrapper script was interpreted by Python due to missing or incorrect Bash shebang, causing a SyntaxError at the exec line.
- Fix/Pattern: Add proper Bash shebang (#!/usr/bin/env bash), ensure LF line endings, make script executable (chmod +x), and optionally invoke via Bash (bash /Users/Ashira/.hermes/scripts/opus-pr-reviewer.sh "$@").
- Prevention: Validate shebang and executable flag before deployment; add pre‑run shebang validation in cron; set SHELL=/bin/bash in crontab.
- Tags: #bash #script-error #opus-pr-reviewer #cron

## 2026-04-27: cost-finops script null model handling bug

## 2026-04-27: Granite-tagger failure
- Context: `granite-tagger.sh` exited with non-zero status.
- Insight: The tagger script failed; potential missing dependencies, permission issues, or environment variables.
- Fix/Pattern: Check script logs, verify execution permissions, ensure required environment variables are set, and add a fallback to hub query.
- Prevention: Add a success check after running `granite-tagger.sh`; if it fails, log the error and continue with hub query.
- Tags: #knowledge-rag #graph #failure
- Context: agent-runner.sh cost-finops role execution failed due to missing model configuration (null model).
- Insight: The ceremony config for the "cost-finops" role has "model": null, which leads agent-runner to pass a literal "None" to the case statement, causing it to invoke claude-bridge with an invalid model and fail (rc=142).
- Fix/Pattern: Update ceremony‑agents.json to use an empty string for null models or add explicit handling for "None"/null values, routing such roles to ai‑fallback instead of claude‑bridge.
- Prevention: Validate the "model" field in ceremony‑agents.json before execution and ensure a fallback path for free‑first roles.
- Tags: #bash #script-error #cost-finops #model-null #ai-fallback

## 2026-04-27: Granite marketing script successful generation
- Context: Ran granite-marketing.sh script to produce marketing positioning for Vanguard product.
- Insight: The script generated a detailed markdown with elevator pitch, value propositions, competitor positioning, hero copy, and objection handlers without manual editing.
- Fix/Pattern: Use granite-marketing.sh for rapid marketing content creation from project README, ensuring consistent messaging and saved time.
- Prevention: Verify the target product name is passed correctly to the script; optionally review generated copy before publishing.
- Tags: #granite #marketing #automation #knowledge-rag
## 2026-04-27: Opus PR Reviewer script exec syntax error
- Context: Data-collection script /Users/Ashira/.hermes/scripts/opus-pr-reviewer.sh failed during cron execution.
- Insight: The wrapper script was interpreted by Python due to missing proper Bash shebang, causing a SyntaxError. Cron may default to /bin/sh which invoked Python.
- Fix/Pattern: Add proper Bash shebang (`#!/usr/bin/env bash`), ensure Unix LF line endings, make script executable (`chmod +x /Users/Ashira/.hermes/scripts/opus-pr-reviewer.sh`), and optionally invoke via Bash explicitly (`bash /Users/Ashira/.hermes/scripts/opus-pr-reviewer.sh "$@"`). Also set `SHELL=/bin/bash` in the crontab.
- Prevention: Add a pre‑run validation step in cron jobs that checks the shebang contains "bash" and that the file is executable before execution.
- Tags: #bash #script-error #opus-pr-reviewer #cron

## 2026-04-27: active-learning wrapper exec error
- Context: Data-collection script /Users/Ashira/.hermes/scripts/active-learning-from-rejects.sh failed.
- Insight: Wrapper script is being interpreted by Python due to missing Bash shebang, causing SyntaxError.
- Fix/Pattern: Ensure script has Bash shebang (#!/usr/bin/env bash), is executable (chmod +x), set SHELL=/bin/bash in crontab, and invoke via bash <script> "$@".
- Prevention: Add pre‑run validation in cron jobs that checks the shebang contains "bash" and file is executable before execution.
- Tags: #script #cron #bash #active-learning #wrapper #error