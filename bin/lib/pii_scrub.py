"""Regex-based PII / secret scrubber for training data rows.

Removes likely-private artifacts before pairs ship to HF Hub:
  - Email addresses
  - Phone numbers (US/intl rough match)
  - IPv4 / IPv6 addresses
  - AWS access key IDs + secret access keys
  - Hugging Face tokens (hf_*)
  - GitHub PATs (ghp_*, gho_*, ghu_*, ghs_*, ghr_*)
  - Slack tokens (xoxb-*, xoxa-*, xoxp-*)
  - Generic OpenAI keys (sk-*)
  - Discord webhook URLs

Discovered 2026-05-02 audit: agent inputs occasionally contained Discord
usernames, repo paths, and Supabase IDs that leaked into adapter outputs.
This module is the row-level filter — call scrub(text) on every prompt
and response before they enter the training-pairs.jsonl stream.

Usage:
    from lib.pii_scrub import scrub, contains_pii
    cleaned = scrub(prompt)
    if contains_pii(response):
        # decide drop vs scrub
        ...
"""
from __future__ import annotations

import re
from typing import Iterable

# Order matters: most-specific patterns first so re.sub does not double-replace.
PII_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    ("aws_secret",     re.compile(r"\b(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])\baws"), "[REDACTED_AWS_SECRET]"),
    ("hf_token",       re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"), "[REDACTED_HF_TOKEN]"),
    ("github_pat",     re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"), "[REDACTED_GH_PAT]"),
    ("openai_key",     re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "[REDACTED_OPENAI_KEY]"),
    ("slack_token",    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    ("discord_webhook", re.compile(r"https?://(?:discord(?:app)?\.com|ptb\.discord\.com|canary\.discord\.com)/api/webhooks/\d+/[A-Za-z0-9_-]+"), "[REDACTED_DISCORD_WEBHOOK]"),
    ("supabase_jwt",   re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"), "[REDACTED_JWT]"),
    ("email",          re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    # IPv4 — exclude obvious version numbers like 1.2.3.4 inside URLs by checking word boundary.
    ("ipv4",           re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"), "[REDACTED_IP]"),
    # IPv6 — only catch full or compressed forms that look like an address (≥2 colons).
    ("ipv6",           re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"), "[REDACTED_IP]"),
    # Phone — international (+CC) or NANP. Conservative to avoid eating version numbers.
    ("phone",          re.compile(r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"), "[REDACTED_PHONE]"),
]

# Patterns that are *informational only* — useful for `contains_pii` checks
# but we do not strip them (would mangle prose). Tracked separately.
INFO_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("discord_username", re.compile(r"\b(?:[A-Za-z0-9_]+#\d{4})\b")),
    ("supabase_ref",     re.compile(r"\b(?:[a-z]{20})\.supabase\.co\b")),
]


def scrub(text: str) -> str:
    """Replace every PII match with a labelled placeholder. Never raises."""
    if not text:
        return text or ""
    out = text
    for _, pattern, replacement in PII_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def contains_pii(text: str) -> bool:
    """Cheap detector — true if any redactable pattern matches."""
    if not text:
        return False
    for _, pattern, _ in PII_PATTERNS:
        if pattern.search(text):
            return True
    return False


def find_pii(text: str) -> list[tuple[str, str]]:
    """Return list of (kind, match_value) for audit logs."""
    if not text:
        return []
    found: list[tuple[str, str]] = []
    for kind, pattern, _ in PII_PATTERNS:
        for m in pattern.finditer(text):
            found.append((kind, m.group(0)))
    return found


def scrub_record(record: dict, fields: Iterable[str] = ("prompt", "response", "chosen", "rejected", "proposal", "refined_proposal", "reject_reason")) -> dict:
    """Return a copy of `record` with every text field passed through scrub()."""
    cleaned = dict(record)
    for f in fields:
        v = cleaned.get(f)
        if isinstance(v, str):
            cleaned[f] = scrub(v)
    return cleaned


if __name__ == "__main__":
    # Smoke test when invoked directly.
    sample = (
        "Email me at alice@example.com or call +1 415-555-0100. "
        "AWS key AKIAIOSFODNN7EXAMPLE, HF hf_abcdefghijklmnopqrstuvwxyz1234. "
        "GitHub ghp_1234567890abcdefghijABCDEFGHIJKLmn. "
        "Webhook https://discord.com/api/webhooks/123456789/abc-DEF_xyz. "
        "Server 192.168.1.42 / 2001:db8::1."
    )
    print("BEFORE:", sample)
    print("AFTER:", scrub(sample))
    print("FINDINGS:", find_pii(sample))
