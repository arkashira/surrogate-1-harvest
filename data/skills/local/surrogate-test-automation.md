---
name: surrogate-test-automation
description: Test automation pattern for surrogate AI model training toolkit project
category: software-development
---

# Surrogate Project Test Automation Skill

## Trigger Conditions
- Working on surrogate project (`/Users/Ashira/axentx/surrogate`)
- Focus on quality assurance and test automation (Q3-Test-Automation cycle)
- Need to validate training data, model outputs, or pipeline integrity
- Preparing for model evaluation or deployment

## Pattern Overview
This skill provides a reusable pattern for implementing test automation in the surrogate AI model training toolkit project. It focuses on validating data quality, pipeline integrity, and model readiness through systematic testing approaches.

## Steps

### 1. Environment Setup
```bash
# Navigate to surrogate project
cd /Users/Ashira/axentx/surrogate

# Verify environment
python --version
pip list | grep -E "(transformers|datasets|peft|accelerate)"

# Create test directory if not exists
mkdir -p tests/{unit,integration,data,model}
```

### 2. Data Quality Testing
```bash
# Validate processed training data
python -c "
import json
from pathlib import Path

data_file = Path('data/processed/surrogate1_training.jsonl')
if not data_file.exists():
    print('ERROR: Training data not found')
    exit(1)

# Count samples and validate structure
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
print(f'✓ Data file: {data_file.absolute()}')
"
```

### 3. Pipeline Integrity Testing
```bash
# Test data preparation script
python scripts/prepare_data.py --help

# Run data preparation with test output
mkdir -p test_output
python scripts/prepare_data.py \
    --input data/raw \
    --output test_output \
    --include-examples

# Validate output
ls -la test_output/
wc -l test_output/surrogate1_training.jsonl
```

### 4. Configuration Validation
```bash
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

# Check required sections
required_sections = ['model', 'datasets', 'training']
missing = [s for s in required_sections if s not in config]
if missing:
    print(f'ERROR: Missing config sections: {missing}')
    exit(1)

print('✓ Training configuration is valid')
print(f'✓ Base model: {config.get(\"model\", {}).get(\"base_model\", \"Not specified\")}')
"
```

### 5. Sample Generation Testing
```bash
# Test sample generation functions
python -c "
from scripts.prepare_data import (
    generate_terraform_samples,
    generate_kubernetes_samples,
    generate_sre_samples,
    generate_architecture_samples
)

# Test each generator
generators = [
    ('Terraform', generate_terraform_samples),
    ('Kubernetes', generate_kubernetes_samples),
    ('SRE', generate_sre_samples),
    ('Architecture', generate_architecture_samples)
]

total_samples = 0
for name, generator in generators:
    try:
        samples = list(generator())
        count = len(samples)
        total_samples += count
        print(f'✓ {name}: {count} samples generated')
        
        # Validate first sample structure
        if samples:
            sample = samples[0]
            required = {'instruction', 'input', 'output', 'category'}
            missing = required - set(sample.keys())
            if missing:
                print(f'ERROR: {name} sample missing fields: {missing}')
                exit(1)
    except Exception as e:
        print(f'ERROR: {name} generator failed: {e}')
        exit(1)

print(f'✓ Total samples from all generators: {total_samples}')
"
```

### 6. Memory System Integration (for swarm-mem pattern)
```bash
# Test surrogate memory system
mkdir -p /Users/Ashira/.surrogate/memory/test_patterns

# Write test pattern to memory
echo '{"pattern": "test_automation_basic", "timestamp": "'$(date -u +"%Y-%m-%dT%H:%M:%SZ")'", "project": "surrogate", "status": "validated"}' > /Users/Ashira/.surrogate/memory/test_patterns/test_automation_basic.json

# Read and verify
cat /Users/Ashira/.surrogate/memory/test_patterns/test_automation_basic.json
```

## Verification Steps
1. All tests should pass without errors
2. Training data should be valid JSONL with required fields
3. Configuration should be parseable YAML with required sections
4. Sample generators should produce valid instruction-following format
5. Memory system should allow read/write operations

## Pitfalls to Avoid
- Don't skip validation of JSONL encoding (ensure UTF-8)
- Don't assume all raw files are processable - the script skips non-code files
- Don't forget to create output directories before writing
- Don't hardcode paths - use relative paths from project root
- Don't ignore category field - it's used for training data organization

## Output Indicators
- ✓ symbols indicate successful validation
- Error messages will specify exact line numbers and issues
- Sample counts should match expected values (4 generators × 2 samples each = 8 from examples)
- Memory files should be valid JSON with timestamp

## Reusability Notes
This pattern can be adapted for:
- Other AI training projects with similar data pipeline structures
- Projects using JSONL format for instruction-following data
- DevSecOps focused AI model training workflows
- Any project needing pre-training data validation

The skill integrates with the surrogate memory system for pattern persistence and can be extended with swarm-mem concepts when that tool becomes available.