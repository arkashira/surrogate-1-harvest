---
name: sbom-drift-gnn-technique
description: Discover and propose AI/ML techniques for SBOM drift detection using fallback tools when web search is blocked.
version: 1.0.0
author: Ashira
---

# Purpose
Automate the process of finding a recent AI/ML technique applicable to a project (e.g., Arkship) when initial web searches encounter CAPTCHAs or rate limits. Includes using internal pipeline helpers, FalkorDB RAG, MCP Context7 library resolution, and writing a decision proposal.

# Steps
1. **Initialize Run Context**
   ```bash
   bash ~/.hermes/scripts/pipeline-helper.sh ai-rd
   ```
   Capture `RUN_ID` from output for naming files.
2. **Gather Domain Context** (optional but recommended)
   ```bash
   ~/.claude/bin/graph-query.sh tag devops
   ```
   Review any relevant docs in the knowledge graph.
3. **Attempt Web Search**
   - Use `browser_navigate` to Bing/DuckDuckGo for the technique query.
   - If a CAPTCHA appears or the request is blocked, **do not retry** the same engine.
4. **Fallback to MCP Context7**
   - Resolve a relevant library ID:
     ```
     mcp_context7_resolve_library_id(libraryName="SBOM drift detection", query="graph neural network 2026")
     ```
   - Query the library for documentation:
     ```
     mcp_context7_query_docs(libraryId="<libraryId>", query="SBOM drift detection GNN 2026")
     ```
5. **Extract Technique Summary**
   - Identify a concise technique name (e.g., "Graph Neural Network for SBOM Drift Detection (SBOM‑GNN)").
   - Assess fit and effort based on project constraints.
6. **Write Decision Proposal**
   - Path: `decisions/<RUN_ID>_ai-rd.md`
   - Content template:
     ```markdown
     Technique: <Name>
     Fit: <High/Medium/Low>
     Effort: <Low/Medium/High>
     ```
   - Use `write_file` to create/overwrite the file.
7. **Log as Backlog Item** (optional) if the technique is promising but not selected.
   - Append JSON line to `backlog.jsonl` with fields `project`, `item`, `size`, `signal`, `source`, `ts`, `status`.

# Pitfalls
- CAPTCHAs on public search engines will block automation; always switch to MCP fallback.
- Ensure the resolved library ID is Context7‑compatible; avoid generic names.
- Verify the technique is from 2026 or later to meet freshness criteria.
- When writing the proposal, use the exact `RUN_ID` captured in step 1 to avoid naming collisions.

# Verification
- Confirm the proposal file exists and contains the three fields.
- Check that `backlog.jsonl` was updated only if needed.

# Reusability
- The skill can be reused for any project needing a recent AI/ML technique discovery, simply change the search query and adjust the domain tag if needed.
