---
name: ai-team-oil-ocr
path: /Users/Ashira/develope/AI/ai-team-oil-ocr
tags: ["project", "codebase", "python", "docker"]
last_indexed: 2026-05-01
type: project
---

# ai-team-oil-ocr

**Path**: `/Users/Ashira/develope/AI/ai-team-oil-ocr`
**Group**: AI
**Languages**: Python
**Frameworks**: Docker
**LOC**: ~14,785
**Deps**: 0

## README
ระบบประมวลผลเอกสารใบกำกับภาษีน้ำมัน (Oil Bills) ด้วย OCR + AI แปลง PDF เป็น JSON โครงสร้าง พร้อมตรวจสอบความถูกต้องด้วย LLM - **ภาพรวมระบบ** - **สถาปัตยกรรมโดยย่อ** - **การติดตั้ง (Python)** - **การใช้งานผ่าน CLI** - **การรันด้วย Docker Compose** - **โครงสร้างโปรเจกต์ (ย่อ)** - **การตั้งค่า .env** - **ลิงก์อ่านเพิ่มเติม** - **Input**: PDF เอกสารภาษีสรรพสามิตน้ำมัน (เช่น ภส.๐๗-๐๑, ภส.๐๕-๐๒ ฯลฯ) - **Process หลัก**:

## Git
- Branch: `main`
- Last commit: 2026-01-16 18:58:11 +0700 Update CI workflow
- Commits (last 30d): 0

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 Dockerfile
📄 README.md
📄 dns-update.json
📄 dns-validation.json
📄 docker-compose.yml
📄 main.py
📁 notebook
  📄 test_ocr_model3_letter.ipynb
📄 pytest.ini
📄 requirements-dev.txt
📄 requirements.txt
📁 src
  📄 __init__.py
  📁 agent_checker
    📄 README.md
    📄 __init__.py
    📄 azure_client.py
    📄 config.py
    📄 convert_pdf_to_base64.py
    📄 file_utils.py
    📁 instruction
    📄 main.py
    📄 pipeline.py
    📄 post_processor.py
    📄 verifier.py
  📁 form_extractor
    📄 README.md
    📄 __init__.py
    📄 config.py
    📄 ex_json_output_05_03.json
    📄 main.py
    📄 pipeline.py
    📁 rule_each_form
  📁 ocr_model
    📄 README.md
    📄 __init__.py
    📄 example_usage.py
    📄 main.py
    📄 ocr_processor.py
    📄 paragraph_extractor.py
    📄 table_converter.py
    📄 utils.py
📄 task-definition.json
📄 test_05_03_order_numbers.py
📄 test_extraction.py
📄 test_extraction_simple.py
📁 tests
  📄 README.md
  📄 __init__.py
  📄 conftest.py
  📄 test_api_endpoints.py
  📄 test_helper_functions.py
  📄 test_models.py
  📄 test_pipeline_per_form_type.py
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
