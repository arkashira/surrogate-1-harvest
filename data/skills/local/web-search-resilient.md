---
name: web-search-resilient
description: Resilient web search that handles CAPTCHA and bot detection by falling back to lite versions and trusted news sites.
version: 1.0.0
author: Hermes
platforms: [macos, linux]
tags: [web, search, captcha, fallback, research]
---

# Resilient Web Search — CAPTCHA Fallback Strategy

When automated web search encounters CAPTCHA or bot detection, use this fallback approach to still gather market signals or information.

## When to Use

- Search queries that return CAPTCHA challenges (e.g., DuckDuckGo, Google bot detection)
- Need to find recent market signals, news, or trends despite automated blocking
- Tasks requiring external web information where standard search APIs fail

## Step-by-Step Fallback Process

### 1. Attempt Standard Search
```bash
browser_navigate "https://duckduckgo.com/html/?q=<QUERY>"
```
or
```bash
browser_navigate "https://lite.duckduckgo.com/lite/"
```
Then fill search box and submit.

### 2. Detect CAPTCHA
After navigation, check for CAPTCHA indicators:
- Text containing "Unfortunately, bots use DuckDuckGo too" or similar
- Presence of challenge elements like "Select all squares containing a duck"
- Image-based challenges (text: "Images not loading?")

### 3. Fallback to Lite Version
If standard search shows CAPTCHA, switch to lite version:
```bash
browser_navigate "https://lite.duckduckgo.com/lite/"
```
Repeat search. Lite version often serves basic HTML without JavaScript challenges.

### 4. Fallback to Trusted Sources\nIf lite version also shows CAPTCHA or fails to yield results, try these fallbacks in order:\n\n### 4a. Internal Knowledge Base\nFirst, check trusted internal sources:\n- Query Hermes knowledge graph: `~/.claude/bin/graph-query.sh tag <relevant-tag>`\n- Search local trend files: `~/Documents/Obsidian Vault/AI-Hub/knowledge/trends-2026/`\n- Review recent decisions: `ls -la <project>/decisions/`\n- Check lessons learned: `~/.claude/memory/lessons_learned.md`\n\n### 4b. Trusted News Sites\nIf internal sources don't have the information, go to trusted technology news sites:\n- The Register (https://www.theregister.com/)\n- Hacker News (https://news.ycombinator.com/)\n- TechCrunch (https://techcrunch.com/)\n- Ars Technica (https://arstechnica.com/)\n\nNavigate to site and use internal search if available, or scan recent headlines for relevant keywords.

### 5. Extract Signal
Once on a content page:
- Use `browser_console` to extract visible text: `document.body.innerText`
- Look for keywords related to query (e.g., "MCP", "security flaw", "DevSecOps")
- Summarize the signal concisely for backlog entry.

## Verification

- Confirm extracted signal is relevant and recent (within last 24-48 hours)
- Ensure no hallucination: stick to verbatim text from page
- If multiple signals found, pick the most relevant to the project domain.

## Example Usage (from arkship DevSecOps task)

Query: "2026 DevSecOps market trends FinOps cloud cost security"
1. Standard DuckDuckGo → CAPTCHA
2. Lite DuckDuckGo → CAPTCHA
3. Navigate to The Register → found article: "Anthropic won't own MCP 'design flaw' putting 200K servers at risk"
4. Signal: MCP design flaw requiring DevSecOps monitoring
5. Added to backlog: `{"item":"MCP security flaw monitoring system","signal":"Anthropic MCP design flaw putting 200K servers at risk requires DevSecOps monitoring"}`

## Pitfalls

- Some sites may also block automated access; vary user-agent if needed (not implemented in basic browser tool)
- Lite version may have limited functionality; fallback to news sites is more reliable
- Always respect robots.txt and rate limits; delay between requests if doing multiple searches.

## Integration with Hermes Pipeline

Use this skill during the "web" step of the axentx dev loop to find market signals for projects like Costinel, Vanguard, Arkship, Surrogate, Workio.

After extracting signal, proceed to terminal step to append to backlog.jsonl.

## Updates

- If new fallback sites prove effective, add to list.
- If browser tool gains CAPTCHA solving capability, simplify flow.