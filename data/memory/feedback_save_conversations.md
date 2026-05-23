---
name: Always Save Conversation Context
description: Every conversation must save what was discussed to Obsidian AI-Hub so other AI tools can continue seamlessly
type: feedback
---

Every conversation MUST save a summary of what was discussed and decided to Obsidian.

**Why:** When Claude hits quota, the user switches to another AI tool (Codex, Gemini, Kimi, GPT). That tool needs to know what happened in the previous conversation to continue without re-explaining.

**How to apply:**
- At the end of every significant conversation, save a session summary to `~/Documents/Obsidian Vault/AI-Hub/sessions/` (create if needed)
- Include: what was done, decisions made, pending work, key file paths changed
- Update relevant knowledge files if new information was learned
- Keep Claude memory (`~/.claude/memory/`) and Obsidian AI-Hub in sync
- Format: markdown with date, topic, summary, and next steps


---

**Graph**: [[../Documents/Obsidian Vault/AI-Hub/patterns/MOC|🧭 Graph Hub]] · [[MEMORY|Memory Index]] · [[knowledge_index|Pattern Index]] · [[lessons_learned|Lessons]]
