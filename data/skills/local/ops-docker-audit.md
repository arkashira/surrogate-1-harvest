---
name: ops-docker-audit
description: Audits Docker-based infrastructure for image size, layer caching, healthchecks, resource limits, and CI integration. Reusable for any project using Dockerfile and docker-compose.
author: Ashira
---

# Ops Docker Audit Skill

## Overview
This skill performs a systematic audit of a project's Docker infrastructure to ensure:
- Minimal image size and effective layer caching
- Presence of healthchecks for all critical services
- Appropriate CPU/memory resource limits
- CI pipeline validation of Docker builds

## Prerequisites
- Access to project directory (`PROJECT_PATH`)
- `docker-compose.yml` and/or `Dockerfile` present
- Bash shell with `cat`, `ls`, `grep`, `sed`
- Ability to edit files (write_file, patch)

## Steps
1. **Locate Docker files**
   ```bash
   ls -lt "$PROJECT_PATH/Dockerfile" "$PROJECT_PATH/docker-compose.yml" "$PROJECT_PATH/backend/Dockerfile" 2>/dev/null
   ```
2. **Read files**
   - Use `read_file` on each found file to inspect content.
3. **Analyze image size & context**
   - Verify that the Dockerfile uses multi‑stage builds.
   - Ensure a `.dockerignore` exists; if not, create one with common exclusions (node_modules, .git, logs, docs, tests, .env, *.md).
4. **Check healthchecks**
   - Scan `docker-compose.yml` for `healthcheck:` entries under each service.
   - For missing services, suggest a healthcheck using `curl` or appropriate CLI.
5. **Add resource limits**
   - Look for a `deploy:` section with `resources: limits:`.
   - If absent, propose CPU/memory limits appropriate to the service (e.g., db: 1 CPU/1 GiB, cache: 0.5 CPU/512 MiB, frontend: 0.5 CPU/256 MiB).
6. **CI integration**
   - Verify existence of a CI workflow file (e.g., `.github/workflows/*.yml`).
   - If missing Docker build step, suggest a snippet that builds the image and prints its size.
7. **Write audit report**
   - Create `decisions/<RUN_ID>_ops.md` containing findings, recommendations, and a timestamp.
8. **Log lesson**
   - Append a structured entry to `~/.claude/memory/lessons_learned.md` with tags `ops devops docker ci`.
9. **Sync knowledge graph**
   - Run `~/.claude/bin/graph-sync.sh` to make the new pattern searchable.

## Pitfalls & Tips
- **Missing `.dockerignore`** leads to unnecessarily large build contexts; always create it before the first build.
- **Healthcheck syntax** must match the service’s entrypoint; use `curl -f` for HTTP services and appropriate commands for others.
- **Resource limits** are soft in plain Docker; they become hard constraints only in orchestrators (Swarm/K8s). Still set them to guide developers.
- **CI runners** often have limited storage; enforce image‑size checks to prevent bloat.

## Verification
- Run `docker build --target builder .` and confirm build completes quickly.
- Execute `docker images` to verify the final image size is reasonable (<200 MB for typical frontend).
- Use `docker-compose up --detach` and ensure all healthchecks report `healthy`.

## Tags
ops devops docker ci
