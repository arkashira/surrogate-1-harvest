---
name: test-code-audit
description: Audit test code for common anti-patterns: inverted skip conditions, assertion-free tests, lying type annotations, magic numbers, implementation-detail testing.
version: 1.0.0
author: HermesSynthesizer
tags: ["testing", "qa", "code-review", "python"]
created_at: 2026-04-23T08:56:58.542334
---

# Test Code Audit

## Rationale
Mythos-coding audits appeared 2x with overlapping findings (async generator type lies, sessionmaker misuse, assertion-free tests, sys.path hacking) — recurring test anti-patterns worth a standardized checklist.

## Steps
1. Check skip conditions for inverted logic (e.g., 'sqlite' in env default that always skips)
2. Verify every test has at least one assertion on observed behavior
3. Flag async generators typed as AsyncSession instead of AsyncGenerator[AsyncSession, None]
4. Detect magic numbers duplicated between tests and code under test — require importing constants
5. Check for private method testing (underscore prefix) and suggest behavior-level alternatives
