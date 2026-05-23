# Global Memory Index (Token-Optimized)

> **Lean auto-load**: Only essentials below auto-load. Heavy files accessed on-demand.

## 🔴 READ FIRST (every session)
- **[knowledge_index.md](knowledge_index.md)** — Pattern matcher. Grep keywords → find known fix.
- **[user_profile.md](user_profile.md)** — Ashira, DevOps/SRE, Dysgraphia, Thai
- **[preferences.md](preferences.md)** — No AI attribution, YOLO, think first

## 📖 Lazy-Loaded (read when relevant to task)

### Code tasks
- [feedback_code_style.md](feedback_code_style.md) — Code style feedback (Write like senior engineer)

### Session/workflow
- [feedback_save_conversations.md](feedback_save_conversations.md) — Save session summaries rule
- [lessons_learned.md](lessons_learned.md) — Past mistakes + prevention (grep before solving)

### Infrastructure/CI-CD
- [devops_pipeline_state.md](devops_pipeline_state.md) — CI/CD state, services, S3 structure
- [cloudformation_stack_knowledge.md](cloudformation_stack_knowledge.md) — All CF stacks, creation flow
- [portable_context.md](portable_context.md) — Excise project AWS/Cognito context

### Cross-tool knowledge
- [ai_hub_reference.md](ai_hub_reference.md) — Pointer to Obsidian AI-Hub (dev-skills, ops-skills, trends-2026, patterns, skills)

## 📂 External Knowledge (Obsidian)

Heavy content lives at `~/Documents/Obsidian Vault/AI-Hub/`:
- `knowledge/` — 10 ref files + `trends-2026/` (6 files)
- `patterns/` — 17 pattern files (MOC is hub)
- `skills/` — 68 community + 9 anthropic mirrored skills

Agents use `grep`/`Read` to load specific files on-demand.

## Query helpers
- `~/.claude/bin/graph-query.sh` — graph DB traversal
- `~/.claude/bin/ask.sh` — vector DB semantic search
