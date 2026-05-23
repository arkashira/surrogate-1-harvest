---
name: excise-codebase
description: Query the excise-wine-proxy codebase semantic index (810 chunks in Chroma). Use when answering questions about this specific project's files, configs, SSO, gateway, nginx, Docker, on-prem deploy.
version: 1.0.0
---

# excise-wine-proxy Codebase RAG

## When to use
- User asks about excise-wine-proxy structure, files, configs, services
- Deploy on-prem questions (docker-compose, scripts, nginx)
- SSO / gateway / api-gateway internal details
- Finding code snippets across the TS/PHP/YAML/shell files

## How to call
From any Hermes agent (terminal tool):

```bash
# Semantic search the codebase
~/.claude/bin/ask-excise.sh "คำถาม"

# Or direct python search (returns top 10 matches)
~/.claude/venv/bin/python -c "
import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
c = chromadb.PersistentClient(path='/Users/Ashira/.claude/chroma-db')
col = c.get_or_create_collection('excise_wine_proxy', embedding_function=DefaultEmbeddingFunction())
r = col.query(query_texts=['YOUR QUERY'], n_results=10)
for doc, m in zip(r['documents'][0], r['metadatas'][0]):
    print(f\"--- {m.get('path')} ---\")
    print(doc[:500])
    print()
"
```

## Project location
`/Users/Ashira/develope/Excise/Wine/excise-wine-proxy`

## Indexed content
- 810 chunks across: TypeScript (89), PHP (96), shell scripts (46), YAML (15), configs (13), Dockerfiles, READMEs
- Services indexed: remote/sso, remote/sso-instant, remote/gateway, nginx, aws scripts, templates

## Re-index after changes
```bash
~/.claude/venv/bin/python /tmp/index-excise.py
```

## Related
- skills/knowledge-rag/SKILL.md (main RAG skill)
- Chroma collection: `excise_wine_proxy`
- Codebase owner: Ashira (Excise Wine Proxy team)
