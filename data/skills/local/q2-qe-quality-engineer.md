---
name: q2-qe-quality-engineer
description: Quality Engineering workflow for surrogate project using swarm-mem for context sharing and pattern storage
category: software-development
---

# Q2-QE-Quality-Engineer Skill: Surrogate Project Quality Assurance

## Trigger Conditions
- Working on surrogate project (`/Users/Ashira/axentx/surrogate`)
- Assigned role: Q2-QE-Quality-Engineer
- Need to establish quality standards, test strategies, and validation processes
- Preparing for model training pipeline validation

## Pattern Overview
This skill provides a structured approach for quality engineering in the surrogate AI model training toolkit project. It leverages the swarm-mem shared memory system to query existing context, establish quality patterns, and share findings with other agents in the swarm.

## Workflow Steps

### 1. Context Discovery via Swarm-Mem
```bash
# Query existing surrogate project context
/Users/Ashira/.claude/bin/swarm-mem query surrogate

# Focus on quality-related entries
/Users/Ashira/.claude/bin/swarm-mem query "quality surrogate"

# Check current cycle context
/Users/Ashira/.claude/bin/swarm-mem query "20260421_0112_surrogate_quality"
```

### 2. Review Existing Quality Contributions
```bash
# Check what other quality engineers have contributed
/Users/Ashira/.claude/bin/swarm-mem agent_history "Q1-QA-Engineer" 5
/Users/Ashira/.claude/bin/swarm-mem agent_history "Q2-QE-Quality-Engineer" 5

# Review leadership and other role inputs
/Users/Ashira/.claude/bin/swarm-mem query "Leadership surrogate" | head -3
/Users/Ashira/.claude/bin/swarm-mem query "Engineering surrogate" | head -3
```

### 3. Establish Test Strategy (Pattern Creation)
Based on discovered context, create a comprehensive test strategy pattern:

```bash
/Users/Ashira/.claude/bin/swarm-mem write pattern "Q2-QE-Quality-Engineer" "Test strategy for surrogate project: [DYNAMIC_CONTENT_BASED_ON_CONTEXT]"
```

Where dynamic content includes:
- Scope: Unit tests for data preparation, integration tests for training pipeline, data validation, LoRA merging/export testing, performance/resource tests
- Frameworks: pytest for Python testing, yamllint for config validation, existing validate_data.py
- Coverage Targets: 80% code coverage for key scripts, 100% for validation/data loading
- Quality Gates: All tests pass, data validation passes, config valid YAML, exported model loadable
- Test Environment: GitHub Actions CI, CPU/GPU testing, fast tests with small subsets
- Test Data: Fixed datasets for unit tests, synthetic data for edge cases
- Reporting: Coverage reports, archived test logs and artifacts

### 4. Validate Project Artifacts
```bash
# Navigate to surrogate project
cd /Users/Ashira/axentx/surrogate

# Validate training data structure
python -c "
import json
from pathlib import Path
data_file = Path('data/processed/surrogate1_training.jsonl')
if not data_file.exists():
    print('ERROR: Training data not found - run prepare_data.py first')
    exit(1)
sample_count = 0
required_fields = {'instruction', 'input', 'output', 'category'}
with open(data_file, 'r', encoding='utf-8') as f:
    for line_num, line in enumerate(f, 1):
        try:
            sample = json.loads(line.strip())
            missing_fields = required_fields - set(sample.keys())
            if missing_fields:
                print(f'ERROR Line {line_num}: Missing fields {missing_fields}')
                exit(1)
            sample_count += 1
        except json.JSONDecodeError as e:
            print(f'ERROR Line {line_num}: Invalid JSON - {e}')
            exit(1)
print(f'✓ Validated {sample_count} training samples')
"

# Validate training configuration
python -c "
import yaml
from pathlib import Path
config_path = Path('configs/training.yaml')
if not config_path.exists():
    print('ERROR: Training config not found')
    exit(1)
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)
required_sections = ['model', 'datasets', 'training']
missing = [s for s in required_sections if s not in config]
if missing:
    print(f'ERROR: Missing config sections: {missing}')
    exit(1)
print('✓ Training configuration is valid')
print(f'✓ Base model: {config.get(\"model\", {}).get(\"base_model\", \"Not specified\")}')
"

# Test data preparation script
python scripts/prepare_data.py --help
```

### 5. Create Reusable Test Patterns
```bash
# Create test directory structure
mkdir -p tests/{unit,integration,data,model,performance}

# Create basic test templates
cat > tests/test_data_validation.py << 'EOF'
"""Test surrogate training data validation"""
import json
import pytest
from pathlib import Path

def test_training_data_exists():
    data_file = Path('data/processed/surrogate1_training.jsonl')
    assert data_file.exists(), "Training data file not found"

def test_training_data_structure():
    data_file = Path('data/processed/surrogate1_training.jsonl')
    required_fields = {'instruction', 'input', 'output', 'category'}
    with open(data_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            sample = json.loads(line.strip())
            missing_fields = required_fields - set(sample.keys())
            assert not missing_fields, f'Line {line_num}: Missing fields {missing_fields}'
EOF

cat > tests/test_config_validation.py << 'EOF'
"""Test surrogate training configuration"""
import yaml
from pathlib import Path

def test_training_config_exists():
    config_path = Path('configs/training.yaml')
    assert config_path.exists(), "Training config not found"

def test_training_config_structure():
    config_path = Path('configs/training.yaml')
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    required_sections = ['model', 'datasets', 'training']
    for section in required_sections:
        assert section in config, f"Missing section: {section}"
EOF
```

### 6. Share Findings Back to Swarm
```bash
# Write quality metrics/findings
/Users/Ashira/.claude/bin/swarm-mem write metric "Q2-QE-Quality-Engineer" "Surrogate project quality assessment: Training data validated, configuration verified, test framework established"

# Write any lessons learned
/Users/Ashira/.claude/bin/swarm-mem write lesson "Q2-QE-Quality-Engineer" "Quality engineering lesson: Always validate data structure before pipeline execution; use swarm-mem to avoid duplicating quality work across agents"
```

## Verification Steps
1. All swarm-mem queries return successfully
2. Existing quality contributions are reviewed and understood
3. Test strategy pattern is stored in swarm-mem with retrievable content
4. Project artifacts (data, config, scripts) validate without errors
5. Basic test templates are created and can be extended
6. Quality metrics and lessons are shared back to swarm memory

## Pitfalls to Avoid
- Don't create quality strategies in isolation - always query swarm-mem first for existing work
- Don't skip validation of JSONL encoding (ensure UTF-8) and required fields
- Don't assume all raw files are processable - the preparation script skips non-code files
- Don't forget to create output directories before running data preparation
- Don't ignore the category field in training data - it's used for organization and filtering
- Don't create redundant tests - check what other quality engineers have already established

## Output Indicators
- Successful swarm-mem queries show relevant context (decisions, tasks, patterns)
- Pattern storage returns confirmation with hash (e.g., "stored 30b26cebad2b [pattern/Q2-QE-Quality-Engineer]")
- Validation scripts print "✓" success messages with counts and file paths
- Test templates are created in the tests/ directory structure
- Metric and lesson storage confirmations show successful storage

## Reusability Notes
This workflow can be adapted for:
- Any AI/ML project using similar data pipeline structures
- Projects needing cross-agent quality coordination via shared memory
- Quality engineering roles in swarm-based development cycles
- Pre-training validation workflows for LLM fine-tuning projects
- Integrating with existing validate_data.py and prepare_data.py scripts

The skill creates a closed-loop quality process: discover context → establish standards → validate artifacts → share findings → improve future cycles.