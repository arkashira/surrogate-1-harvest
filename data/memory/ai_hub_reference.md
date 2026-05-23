---
name: AI Knowledge Hub Reference
description: Location and contents of the shared Obsidian AI-Hub knowledge base used by all AI tools
type: reference
---

Universal AI Knowledge Hub lives at `~/Documents/Obsidian Vault/AI-Hub/`.

**Knowledge files** (`AI-Hub/knowledge/`):
- `dev-skills.md` — Full-stack dev patterns, naming, type safety, testing, security
- `ops-skills.md` — DevOps/SRE/Cloud patterns, AWS, Docker, K8s, CI/CD, monitoring
- `cloudformation.md` — All CF stacks, creation flow, S3 artifact structure, naming
- `architecture.md` — Agent teams, project structure, decision framework
- `devops-repos.md` — Complete map of 30+ repos in ~/develope/DevOps/
- `terraform.md` — All Terraform configs (AWS, GCP, Azure, VPN, IAM, security remediation)
- `workspace-map.md` — Full workspace map of ~/develope/ and ~/axentx/
- `excise-services.md` — Detailed Excise service internals (API endpoints, models, Docker)

**Tool configs** (`AI-Hub/tools/`):
- `claude.md`, `codex.md`, `gemini.md`, `failover.md`

**Entry point**: `AI-Hub/CONTEXT.md` — universal context for any AI tool

**Sync**: Claude memory at `~/.claude/memory/` is symlinked into Obsidian at `~/Documents/Obsidian Vault/Claude Memory/`. Bidirectional.

**How to apply**: When starting a new chat or needing project context, read CONTEXT.md first. For specific domains, read the relevant knowledge file. All AI tools (Claude, Codex, Gemini, Kimi, GPT) can access these files.


---

**Graph**: [[../Documents/Obsidian Vault/AI-Hub/patterns/MOC|🧭 Graph Hub]] · [[MEMORY|Memory Index]] · [[knowledge_index|Pattern Index]] · [[lessons_learned|Lessons]]
