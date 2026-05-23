---
name: tb-ai-mfa-portal-gen2
path: /Users/Ashira/develope/DevOps/azure-terraform/tb-ai-mfa-portal-gen2
tags: ["project", "codebase", "docker"]
last_indexed: 2026-05-01
type: project
---

# tb-ai-mfa-portal-gen2

**Path**: `/Users/Ashira/develope/DevOps/azure-terraform/tb-ai-mfa-portal-gen2`
**Group**: azure-terraform
**Languages**: unknown
**Frameworks**: Docker
**LOC**: ~32,882
**Deps**: 0

## README
ระบบ Portal อัจฉริยะสำหรับกระทรวงการต่างประเทศ (Ministry of Foreign Affairs) พร้อม AI Workflow Automation ผ่าน Azure AI Foundry - [ภาพรวมระบบ](#ภาพรวมระบบ) - [Tech Stack](#tech-stack) - [โครงสร้างโปรเจกต์](#โครงสร้างโปรเจกต์) - [Modules](#modules) - [e-Saraban](#e-saraban--document-intelligence) - [Task Management](#task-<REDACTED>) - [E-Booking](#e-booking--vehicle--calendar) - [Workflow Engine](#workflow-engine)

## Git
- Branch: `main`
- Last commit: 2026-03-20 15:00:30 +0700 Revert to SQLite database configuration
- Commits (last 30d): 0

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 CLAUDE.md
📄 Dockerfile
📄 README.md
📁 attachments
  📄 14012026_ThinkBIT_MFA_Discovery (1).pdf
  📄 MFA_Command_Center_Pipeline_Architecture_Brief_SQLite_Demo.md
  📄 MFA_Demo_Architecture_Brief.md
  📄 MFA_Pipeline_Architecture_Brief 2(1).md
  📄 MFA_Pipeline_Architecture_Brief 2.md
  📄 Portal_Gen2_Mockup_V2.html
  📄 canvas_link.txt
  📄 demo.html
  📄 document.doc
  📄 index.html
  📄 index_old.html
📁 backend
  📄 Dockerfile
  📁 app
    📄 __init__.py
    📁 auth
    📄 config.py
    📄 db.py
    📄 dependencies.py
    📁 integrations
    📄 main.py
    📁 models
    📁 modules
    📁 pipeline
    📁 routes
    📁 workflow_engine
  📄 requirements.txt
  📁 tests
    📄 __init__.py
    📄 conftest.py
    📄 test_conditions.py
    📄 test_engine.py
    📄 test_flow_control.py
    📄 test_rbac.py
    📄 test_registry.py
    📄 test_result.py
    📄 test_state.py
    📄 test_templates.py
📄 current_plan.md
📁 database
  📄 mfa_portal.db
  📄 schema_v2.sql
📄 detail_module.md
📄 docker-compose.yml
📁 docs
  📄 api_wiring_review.md
  📄 database_to_ui_mapping.md
  📄 devils_advocate_report.md
  📄 frontend-test-checklist.md
  📁 initial_doc
    📄 MFA_Command_Center_Pipeline_Architecture_Brief_SQLite_Demo.md
    📄 MFA_Wiring_Plan_Phase3_Update.md
    📁 add_dashboard_page
    📁 front_end
  📄 review_report.md
  📁 screenshots
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
