# Agent Team Architecture & Workflow

> How AI assistants should work on this machine. Applicable to all AI tools.

## Agent Swarm Pattern

For complex tasks, use a team of specialized agents working in parallel:

### Roles
| Role | Responsibility |
|------|---------------|
| **orchestrator** | Team lead. Breaks work, spawns agents, coordinates, reports |
| **dev** | Full-stack development. Frontend, backend, API, database, mobile |
| **ops** | Infrastructure, CI/CD, Docker, K8s, AWS, security, monitoring |
| **architect** | System design, ADRs, requirements, trade-offs, tech decisions |
| **qa** | Testing strategy, test automation, quality gates |
| **reviewer** | Final quality gate. Reviews ALL deliverables before completion |

### When to Use Agent Swarm
- Code changes across multiple files
- Infrastructure work
- Feature development
- Debugging that requires investigation
- Review or audit tasks

### When NOT to Use (Just Do It)
- Simple typo fixes
- Quick questions / explanations
- Single-file changes
- Reading/explaining code

## Tool-Specific Agent Invocation

### Claude Code
```
Agent(
  name="orchestrator",
  subagent_type="orchestrator",
  mode="bypassPermissions",
  prompt="[task description]"
)
```

### Codex
```
Multi-agent mode enabled in config.toml:
[features]
multi_agent = true
```

### Gemini CLI
Uses sandbox mode. Agent patterns via function calling.

### General Rule
Each AI tool has its own agent mechanism. The KEY is:
1. Break complex work into parallel subtasks
2. Each subtask gets a specialist
3. All work passes through review before completion
4. Results merge back to user

---

## Decision Framework

### When to Plan vs Execute
- **Plan first**: >3 files changed, new feature, architecture change
- **Execute directly**: bug fix, config change, known pattern

### When to Ask vs Act
- **Act autonomously**: clear intent, reversible, low risk
- **Ask first**: destructive ops, ambiguous intent, production changes

### Code Review Checklist
1. Does it work? (correctness)
2. Is it secure? (OWASP top 10)
3. Is it performant? (N+1, unbounded lists, missing indexes)
4. Is it maintainable? (naming, structure, complexity)
5. Is it tested? (behavior tests, edge cases)
6. Is it observable? (logging, metrics, tracing)

---

## Project Structure Convention

```
~/develope/
├── Excise/
│   ├── Wine/          # Wine project services
│   │   ├── excise-wine-authen/
│   │   ├── excise-wine-nodejs-api/
│   │   ├── excise-wine-go-api/
│   │   └── excise-wine-proxy/
│   └── Car/           # Car project services
│       ├── excise-car-backend/
│       └── excise-car-cron/
├── DevOps/
│   ├── thinkbit-devops-material/   # CF templates, buildspec templates
│   ├── thinkbit-devops-modules/    # Per-service params + buildspecs
│   ├── thinkbit-devops-jenkins/    # Jenkins server
│   └── ...other DevOps repos
└── ...other projects
```
