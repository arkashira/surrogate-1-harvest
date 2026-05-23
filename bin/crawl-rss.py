#!/usr/bin/env python3
"""Generic RSS/Atom crawler for knowledge ingestion.

Reads feed URLs from FEEDS env or default list, parses entries, writes JSONL
to output file. Only writes entries not seen before (dedup by URL).

Usage (from bash):
    OUT=/tmp/out.jsonl python3 ~/.surrogate/bin/crawl-rss.py

All feeds VERIFIED to return 200 as of 2026-04-19. Failures are logged,
not fatal — one bad feed doesn't kill the rest.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

FEEDS: list[tuple[str, str]] = [
    # (category, url)
    # --- Cloud architecture ---
    ("aws-architecture", "https://aws.amazon.com/blogs/architecture/feed/"),
    ("aws-security",     "https://aws.amazon.com/blogs/security/feed/"),
    ("aws-devops",       "https://aws.amazon.com/blogs/devops/feed/"),
    ("aws-containers",   "https://aws.amazon.com/blogs/containers/feed/"),
    ("aws-compute",      "https://aws.amazon.com/blogs/compute/feed/"),
    ("aws-general",      "https://aws.amazon.com/blogs/aws/feed/"),
    ("aws-whatsnew",     "https://aws.amazon.com/about-aws/whats-new/recent/feed/"),
    ("aws-security-bulletin", "https://aws.amazon.com/security/security-bulletins/feed/"),
    ("azure",            "https://azurecomcdn.azureedge.net/en-us/blog/feed/"),
    ("gcp-architecture", "https://cloud.google.com/blog/topics/developers-practitioners/rss"),
    ("gcp-releases",     "https://cloud.google.com/feeds/gcp-release-notes.xml"),
    # --- K8s / CNCF ---
    ("kubernetes",       "https://kubernetes.io/feed.xml"),
    ("cncf",             "https://www.cncf.io/feed/"),
    ("cncf-announcements","https://cncf.io/announcement/feed/"),
    ("hashicorp",        "https://www.hashicorp.com/blog/feed.xml"),
    # --- Security ---
    ("cisa-advisories",  "https://www.cisa.gov/uscert/ncas/current-activity.xml"),
    ("openssf",          "https://www.openssf.org/feed/"),
    ("cisco-security",   "https://feeds.feedburner.com/CiscoBlogSecurity"),
    # --- AI providers ---
    ("openai",           "https://openai.com/news/rss.xml"),
    ("google-ai",        "https://blog.google/rss/"),
    ("deepmind",         "https://deepmind.google/blog/rss.xml"),
    # --- Engineering practice ---
    ("github-engineering","https://github.blog/engineering.atom"),
    ("martinfowler",     "https://feeds.feedburner.com/martinfowler"),
    ("thoughtworks",     "https://feeds.feedburner.com/thoughtworks"),
    ("jetbrains",        "https://blog.jetbrains.com/feed/"),
    # --- Community discussion ---
    ("hackernews-best",  "https://hnrss.org/best"),
    ("lobsters",         "https://lobste.rs/rss"),
    ("devto",            "https://dev.to/feed"),
    # --- Go language ---
    ("golang-blog",      "https://go.dev/blog/feed.atom"),
    ("golang-weekly",    "https://golangweekly.com/rss/1p87oj6"),
    # --- AI agents & tooling ---
    ("anthropic",        "https://www.anthropic.com/rss.xml"),
    ("huggingface",      "https://huggingface.co/blog/feed.xml"),
    ("langchain",        "https://blog.langchain.dev/rss/"),
    # --- FinOps & cost engineering ---
    ("finops-foundation","https://www.finops.org/feed/"),
    ("infracost",        "https://www.infracost.io/blog/rss.xml"),
    # --- Security / compliance ---
    ("prowler",          "https://github.com/prowler-cloud/prowler/releases.atom"),
    ("snyk",             "https://snyk.io/blog/feed/"),
    ("aquasec",          "https://www.aquasec.com/blog/feed/"),
    # --- Platform engineering ---
    ("platformengineering", "https://platformengineering.org/blog/rss.xml"),
    ("infoq-devops",     "https://feed.infoq.com/devops/"),
    ("dzone-devops",     "https://dzone.com/devops/rss.xml"),
    # --- Startup / product ---
    ("ycombinator",      "https://www.ycombinator.com/blog/rss"),
    ("paulgraham",       "https://www.aaronsw.com/2002/feeds/pgessays.rss"),
    ("producthunt",      "https://www.producthunt.com/feed"),
    # --- TypeScript / Node.js ---
    ("nodejs-blog",      "https://nodejs.org/en/feed/blog.xml"),
    ("typescript-blog",  "https://devblogs.microsoft.com/typescript/feed/"),
]

OUT_PATH = os.environ.get("OUT", "/tmp/rss-crawl.jsonl")
SEEN_PATH = os.environ.get("SEEN", os.path.expanduser("~/.surrogate/.rss-seen.json"))
MAX_ENTRIES_PER_FEED = int(os.environ.get("MAX_PER_FEED", "10"))
TIMEOUT = int(os.environ.get("TIMEOUT", "15"))

RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return HTML_TAG_RE.sub("", text or "").strip()


def load_seen() -> set[str]:
    try:
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    os.makedirs(os.path.dirname(SEEN_PATH) or ".", exist_ok=True)
    # Cap seen set to 10k entries (oldest gc'd implicitly by order)
    with open(SEEN_PATH, "w") as f:
        json.dump(list(seen)[-10000:], f)


def fetch(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "axentx-crawler/1.0 (knowledge-ingestion)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  [err] {url[:60]}: {e}", file=sys.stderr)
        return None


def parse_entries(raw: bytes) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  [parse-err] {e}", file=sys.stderr)
        return []

    out: list[dict] = []
    # Atom feeds
    if root.tag.endswith("feed"):
        for entry in root.findall("atom:entry", RSS_NS)[:MAX_ENTRIES_PER_FEED]:
            title = (entry.findtext("atom:title", "", RSS_NS) or "").strip()
            link_el = entry.find("atom:link", RSS_NS)
            url = link_el.get("href") if link_el is not None else ""
            summary = (
                entry.findtext("atom:summary", "", RSS_NS)
                or entry.findtext("atom:content", "", RSS_NS)
                or ""
            )
            published = entry.findtext("atom:published", "", RSS_NS)
            if title and url:
                out.append({
                    "title": title,
                    "url": url,
                    "summary": strip_html(summary)[:500],
                    "published": published,
                })
        return out

    # RSS 2.0
    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item")[:MAX_ENTRIES_PER_FEED]:
            title = (item.findtext("title", "") or "").strip()
            url = (item.findtext("link", "") or "").strip()
            description = item.findtext("description", "") or ""
            content_encoded = item.find("content:encoded", RSS_NS)
            if content_encoded is not None and content_encoded.text:
                description = content_encoded.text
            pub_date = item.findtext("pubDate", "") or ""
            if title and url:
                out.append({
                    "title": title,
                    "url": url,
                    "summary": strip_html(description)[:500],
                    "published": pub_date,
                })
    return out


def main() -> int:
    seen = load_seen()
    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    today = dt.date.today().isoformat()
    written_total = 0
    per_category: dict[str, int] = {}

    with open(OUT_PATH, "a") as out:
        for category, url in FEEDS:
            print(f"→ {category}: {url[:70]}", file=sys.stderr)
            raw = fetch(url)
            if not raw:
                continue
            entries = parse_entries(raw)
            new_count = 0
            for e in entries:
                if e["url"] in seen:
                    continue
                seen.add(e["url"])
                record = {
                    "title": e["title"],
                    "url": e["url"],
                    "summary": e["summary"],
                    "source": category,
                    "published": e.get("published", ""),
                    "date_crawled": today,
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                new_count += 1
            per_category[category] = new_count
            written_total += new_count
            print(f"    +{new_count} new", file=sys.stderr)

    save_seen(seen)
    print(f"\n[done] {written_total} new entries across {len(per_category)} feeds", file=sys.stderr)
    print(json.dumps({"feeds": len(per_category), "new_entries": written_total, "per_category": per_category}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
