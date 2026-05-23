---
name: test-code-mythos-audit
description: Review test files for anti-patterns: inverted skips, assertion-free tests, private-API testing, magic numbers, deprecated APIs, stale docstrings.
version: 1.0.0
author: HermesSynthesizer
tags: ["testing", "code-review", "quality"]
created_at: 2026-04-23T08:54:55.399084
---

# Test Code Mythos Audit

## Rationale
The mythos-coding audit surfaced a reusable checklist of test anti-patterns (stringly-typed skip conditions, missing assertions, testing private methods, magic-number drift, wrong async annotations, docstring-code drift) that applies to any pytest/async test suite.

## Steps
1. Scan skipif/skip conditions for stringly-typed substring checks — replace with parsed/typed checks
2. Flag any test function whose body has no assert statement
3. Flag tests that call underscore-prefixed (private) methods directly
4. Flag hardcoded threshold/magic numbers in parametrize — require import from module under test
5. Verify async fixture annotations (AsyncGenerator vs AsyncSession) and docstring↔test coverage alignment
