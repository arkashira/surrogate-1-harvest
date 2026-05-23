---
name: surrogate-quality-assurance
description: Quality assurance pattern for surrogate AI model training toolkit project with H1-HR role focus, using swarm-mem and memory tools
category: software-development
---

# Surrogate Project Quality Assurance Skill (H1-HR Focus)

## Trigger Conditions
- Working on surrogate project (`/Users/Ashira/axentx/surrogate`)
- Focus on quality assurance (H1-HR role in swarm context)
- Specific cycle ID provided (e.g., 20260421_0112_surrogate_quality)
- Need to ensure training data quality, pipeline integrity, and model readiness
- Preparing for quality gates, test strategy definition, or quality metric documentation

## Pattern Overview
This skill provides a reusable pattern for implementing quality assurance in the surrogate AI model training toolkit project, specifically tailored for the H1-HR role (Head of Quality/Quality Engineer in the agent swarm). It integrates swarm-mem for shared knowledge storage and retrieval, uses the surrogate memory tool for pattern persistence, and includes systematic validation of data, configuration, and pipeline steps.

## Steps

### 1. Initialize Quality Cycle
```bash
# Navigate to surrogate project
cd /Users/Ashira/axentx/surrogate

# Record quality focus decision in swarm memory
~/.claude/bin/swarm-mem write decision H1-HR "Focusing on quality for surrogate project in cycle <CYCLE_ID>"

# Verify recording
~/.claude/bin/swarm-mem query "quality" | head -5
```
Replace `<CYCLE_ID>` with the actual cycle ID (e.g., 20260421_0112_surrogate_quality).

### 2. Retrieve Quality-Related Patterns
```bash
# Query swarm memory for quality-related decisions, designs, and lessons
~/.claude/bin/swarm-mem query "quality test strategy coverage gates" --limit 10
~/.claude/bin/swarm-mem query "quality assurance validation" --limit 10

# Optionally, get recent quality entries from last 24 hours
~/.claude/bin/swarm-mem recent 24 | grep -i quality
```

### 3. Validate Training Data
```bash
# Run existing data validation script
python validate_data.py

# Alternatively, run comprehensive validation from surrogate-test-automation skill
# (See related skill for detailed data validation steps)
```

### 4. Ensure Pipeline Integrity
```bash
# Test data preparation script with quality focus
mkdir -p test_output_quality
python scripts/prepare_data.py \
    --input data/raw \
    --output test_output_quality \
    --include-examples

# Validate output
echo "Generated samples:"
wc -l test_output_quality/surrogate1_training.jsonl
echo "Category breakdown:"
grep -o '"category":"[^"]*"' test_output_quality/surrogate1_training.jsonl | sort | uniq -c
```

### 5. Store Quality Patterns in Memory
```bash
# Create directory for quality patterns in surrogate memory
mkdir -p /Users/Ashira/.surrogate/memory/quality_patterns

# Write a quality pattern (example: test strategy decision)
cat > /Users/Ashira/.surrogate/memory/quality_patterns/test_strategy_$(date -u +%Y%m%d_%H%M%S).json << EOF
{
  "pattern": "test_strategy_definition",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "project": "surrogate",
  "role": "H1-HR",
  "cycle_id": "<CYCLE_ID>",
  "status": "defined",
  "details": {
    "frameworks": ["pytest", "yaml-validation"],
    "coverage_target": {"unit": 80, "integration": 60},
    "quality_gates": ["data_validation", "config_check", "sample_generation"]
  }
}
EOF
```

### 6. Create Quality Metrics Document (Safe Alternative)
```bash
# Create a quality metrics document (don't modify existing files)
echo "## Quality Metrics for Cycle <CYCLE_ID>" > QUALITY_METRICS_<CYCLE_ID>.md
echo "- Data samples validated: $(wc -l < data/processed/surrogate1_training.jsonl)" >> QUALITY_METRICS_<CYCLE_ID>.md
echo "- Configuration valid: $(python -c \"import yaml; yaml.safe_load(open('configs/training.yaml')); echo 'yes'\")" >> QUALITY_METRICS_<CYCLE_ID>.md
```
Replace `<CYCLE_ID>` with the actual cycle ID.

### 7. Verify and Communicate
```bash
# Check swarm memory for stored quality decision
~/.claude/bin/swarm-mem query "Focusing on quality for surrogate project" --limit 1

# List stored quality patterns
ls -la /Users/Ashira/.surrogate/memory/quality_patterns/

# Show generated metrics document
cat QUALITY_METRICS_<CYCLE_ID>.md
```

## Verification Steps
1. Swarm memory contains the H1-HR decision for the current cycle.
2. Quality-related queries return relevant past patterns (if any).
3. Data validation passes without errors.
4. Data preparation script runs and generates expected samples.
5. Quality patterns are stored in surrogate memory with proper JSON structure.
6. Quality metrics document is created with cycle-specific information.

## Pitfalls to Avoid
- Don't forget to replace `<CYCLE_ID>` with the actual cycle ID in commands.
- Don't skip validation of JSONL encoding (ensure UTF-8) when processing raw files.
- Don't assume all raw files are processable - the script skips non-code files.
- Don't forget to create output directories before writing.
- Don't hardcode paths - use relative paths from project root.
- Don't ignore the category field in training data - it's used for organization.
- Don't store invalid JSON in memory patterns - validate before writing.

## Output Indicators
- ✓ Swarm memory storage confirmation: `stored <ID> [decision/H1-HR]`
- ✓ Query results show relevant quality-related entries with scores and timestamps.
- ✓ Validation output: `✓ Validated X training samples`
- ✓ Data generation output: `✅ Generated Y training samples` with category breakdown.
- ✓ Memory files created in `/Users/Ashira/.surrogate/memory/quality_patterns/` with valid JSON.
- ✓ Quality metrics document created: `QUALITY_METRICS_<CYCLE_ID>.md` with cycle-specific metrics.

## Reusability Notes
This pattern can be adapted for:
- Other AI training projects with similar data pipeline structures.
- Projects requiring quality assurance tracking in agent swarms.
- Any project needing to integrate swarm-mem for decision tracking and memory tool for pattern persistence.
- Roles beyond H1-HR by changing the role parameter in swarm-mem commands.
- Different cycle IDs by updating the placeholder.

## Related Skills
- surrogate-test-automation: For detailed test automation procedures.
- systematic-debugging: For root cause analysis if quality issues are found.
- plan: For creating quality assurance plans before execution.

## Changelog
- 2026-04-21: Initial creation for surrogate project quality assurance with H1-HR focus.