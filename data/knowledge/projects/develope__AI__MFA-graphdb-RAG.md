---
name: MFA-graphdb-RAG
path: /Users/Ashira/develope/AI/MFA-graphdb-RAG
tags: ["project", "codebase", "docker"]
last_indexed: 2026-05-01
type: project
---

# MFA-graphdb-RAG

**Path**: `/Users/Ashira/develope/AI/MFA-graphdb-RAG`
**Group**: AI
**Languages**: unknown
**Frameworks**: Docker
**LOC**: ~64,561
**Deps**: 0

## README
Agent-Native Database Architecture for searching and monitoring international treaties. ```bash docker run -d \ --name mfa-postgres \ -e POSTGRES_DB=mfa_treaties \ -e POSTGRES_USER=mfa \ -e POSTGRES_PASSWORD=mfa_pass \ -p 5432:5432 \ -v postgres_data:/var/lib/postgresql/data \ ankane/pgvector:latest ``` ```bash conda create -n mfa-treaty python=3.11 conda activate mfa-treaty pip install -r requirements.txt cp .env.example .env

## Git
- Branch: `main`
- Last commit: 2026-02-26 16:40:23 +0700 upload .yml
- Commits (last 30d): 0

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 1
📄 AGENT_ARCHITECTURE_STANDARDIZATION.md
📄 Dockerfile.backend
📄 EOF
📄 MY_NOTE.md
📄 README-DOCKER.md
📄 README.md
📄 agent_search_results.md
📁 archive
  📁 setup
    📄 01_postgres_setup.ipynb
    📄 02_data_ingestion.ipynb
    📄 03_search_api.ipynb
    📄 ARCHITECTURE.md
  📁 specs
    📄 README.md
📁 backend
  📄 __init__.py
  📄 agent_api.py
  📄 agent_cache.py
  📄 agent_loop.py
  📄 agent_loop_implementations.py
  📄 agent_search.py
  📄 agent_tools.py
  📁 agents
    📄 __init__.py
    📄 approval_notes_agent.py
    📄 assessment.py
    📄 base_agent.py
    📄 comparison_agent.py
    📄 coordinator.py
    📄 devils_advocate.py
    📄 document_analyzer.py
    📄 document_review_agent.py
    📄 error_guide.py
    📄 mou_drafting_agent.py
    📄 procurement_study_agent.py
    📄 quality_assurance.py
    📄 scenario_generator.py
    📄 suggestive_questions.py
    📄 summarization_specialist.py
    📄 test_assessment.py
    📄 training_coordinator.py
    📄 translation_specialist.py
    📄 treaty_review_agent.py
  📄 api_demo.py
  📄 api_search.py
  📁 data
    📄 __init__.py
    📄 error_patterns.py
    📄 scenario_templates.py
    📄 workflow_templates.py
  📄 db.py
  📁 ingestion
    📄 __init__.py
    📄 ai_metadata.py
    📄 azure_ocr.py
    📄 date_extraction_config.py
    📄 pipeline.py
  📄 main.py
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
