#!/usr/bin/env python3
"""axentx multi-language pain stream — international forum harvester.

Pulls pain signals from non-English/non-Reddit forums worldwide:
  Russia:    Habr.com, Pikabu, VC.ru
  Japan:     Qiita, Zenn, Note.com
  China:     V2EX, 36Kr (RSS proxy)
  Brazil:    TabNews, Hashnode (BR tag)
  Vietnam:   Tinhte
  India:     Hashnode (India), Telegraph India
  SEA:       e27, KrAsia, DealStreetAsia, Vulcan Post
  Spain:     Genbeta, Xataka
  Korea:     Brunch, Velog (Korean dev blog)
  Germany:   t3n
  France:    Developpez
  Pantip:    More rooms (Suanlumpini, Silom, BluePlanet,
             Sinthorn, Klaibaan, Mahawaytong, Greenzone, etc.)
  StackExchange:  dev/sysadmin/dba/security feeds
  B2B reviews:    G2, Trustpilot (limited RSS)
"""
import datetime, hashlib, html, json, os, random, re, signal, sys
import time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import write_item, log, daemon_loop, new_item

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/121.0.0.0 Safari/537.36")

# Source list — (name, url, parser, lang, category)
SOURCES = [
    # ─── Russia / Russian dev ─────────────────────────────────────────
    ("habr",        "https://habr.com/ru/rss/all/all/?fl=ru",  "rss", "ru", "dev"),
    ("habr-en",     "https://habr.com/en/rss/all/all/",        "rss", "en", "dev"),
    ("vcru",        "https://vc.ru/rss",                        "rss", "ru", "biz"),
    # ─── Japan ────────────────────────────────────────────────────────
    ("qiita",       "https://qiita.com/popular-items/feed",     "rss", "ja", "dev"),
    ("zenn-tech",   "https://zenn.dev/feed",                    "rss", "ja", "dev"),
    ("note-tech",   "https://note.com/topic/プログラミング/feed", "rss", "ja", "tech"),
    # ─── China dev (V2EX is famous Chinese dev forum) ────────────────
    ("v2ex",        "https://www.v2ex.com/index.xml",           "rss", "zh", "dev"),
    # ─── Brazil ───────────────────────────────────────────────────────
    ("tabnews",     "https://www.tabnews.com.br/recentes/rss",  "rss", "pt", "dev"),
    # ─── Vietnam ──────────────────────────────────────────────────────
    ("tinhte",      "https://tinhte.vn/rss/",                   "rss", "vi", "tech"),
    # ─── India / SEA startup ──────────────────────────────────────────
    ("e27",         "https://e27.co/feed/",                     "rss", "en", "sea-startup"),
    ("krasia",      "https://kr-asia.com/feed",                 "rss", "en", "sea-startup"),
    ("vulcanpost",  "https://vulcanpost.com/feed/",             "rss", "en", "sea-tech"),
    # ─── Germany ──────────────────────────────────────────────────────
    ("t3n",         "https://t3n.de/rss.xml",                   "rss", "de", "tech"),
    # ─── France ───────────────────────────────────────────────────────
    ("frenchdev",   "https://www.developpez.com/index/rss",     "rss", "fr", "dev"),
    # ─── Spain ────────────────────────────────────────────────────────
    ("genbeta",     "https://www.genbeta.com/feedburner.xml",   "rss", "es", "tech"),
    ("xataka",      "https://www.xataka.com/index.xml",         "rss", "es", "tech"),
    # ─── Korea ────────────────────────────────────────────────────────
    ("velog-trend", "https://v2.velog.io/rss/@trending",        "rss", "ko", "dev"),
    # ─── Pantip more rooms ────────────────────────────────────────────
    ("pantip-sinthorn",     "https://pantip.com/forum/sinthorn/feed",     "rss", "th", "consumer-pain"),
    ("pantip-klaibaan",     "https://pantip.com/forum/klaibaan/feed",     "rss", "th", "general"),
    ("pantip-mahawaytong",  "https://pantip.com/forum/mahawaytong/feed",  "rss", "th", "edu"),
    ("pantip-greenzone",    "https://pantip.com/forum/greenzone/feed",    "rss", "th", "consumer"),
    ("pantip-jatujak",      "https://pantip.com/forum/jatujak/feed",      "rss", "th", "biz"),
    # ─── Stack Exchange — broad pain across niches ───────────────────
    ("stackoverflow",     "https://stackoverflow.com/feeds/tag/python",        "rss", "en", "dev-pain"),
    ("se-serverfault",    "https://serverfault.com/feeds",                     "rss", "en", "sysadmin-pain"),
    ("se-superuser",      "https://superuser.com/feeds",                       "rss", "en", "tech-pain"),
    ("se-dba",            "https://dba.stackexchange.com/feeds",               "rss", "en", "db-pain"),
    ("se-security",       "https://security.stackexchange.com/feeds",          "rss", "en", "sec-pain"),
    ("se-pm",             "https://pm.stackexchange.com/feeds",                "rss", "en", "pm-pain"),
    ("se-startups",       "https://startups.stackexchange.com/feeds",          "rss", "en", "startup-pain"),
    # ─── Hashnode (developer blog, multilingual) ─────────────────────
    ("hashnode",          "https://hashnode.com/rss",                          "rss", "en", "dev"),
    # ─── Substack pain (writers complain) ────────────────────────────
    ("substack-tech",     "https://substack.com/inbox/explore?tag=technology", "rss", "en", "tech-essay"),
    # ─── BetaList alternatives — early stage SaaS pain ──────────────
    ("microconf",         "https://microconf.com/feed/",                       "rss", "en", "saas-pain"),
    ("ihrss",             "https://www.indiehackers.com/feed.xml",             "rss", "en", "ih-pain"),
    # ─── # 2026-05-08 expanded pain sources ────────────────────────
    # 14 high-quality additions covering tech-specific + biz pain.

    # Stack Overflow tag-specific (newest unanswered = solvable pain).
    # Each tag = independent rate-limit bucket; pick high-volume + high-pain.
    ("so-kubernetes",     "https://stackoverflow.com/feeds/tag/kubernetes",    "rss", "en", "k8s-pain"),
    ("so-react",          "https://stackoverflow.com/feeds/tag/reactjs",       "rss", "en", "react-pain"),
    ("so-nextjs",         "https://stackoverflow.com/feeds/tag/next.js",       "rss", "en", "nextjs-pain"),
    ("so-typescript",     "https://stackoverflow.com/feeds/tag/typescript",    "rss", "en", "ts-pain"),
    ("so-aws",            "https://stackoverflow.com/feeds/tag/amazon-web-services", "rss", "en", "aws-pain"),
    ("so-docker",         "https://stackoverflow.com/feeds/tag/docker",        "rss", "en", "docker-pain"),
    ("so-terraform",      "https://stackoverflow.com/feeds/tag/terraform",     "rss", "en", "iac-pain"),
    ("so-postgresql",     "https://stackoverflow.com/feeds/tag/postgresql",    "rss", "en", "pg-pain"),
    ("so-rust",           "https://stackoverflow.com/feeds/tag/rust",          "rss", "en", "rust-pain"),
    ("so-go",             "https://stackoverflow.com/feeds/tag/go",            "rss", "en", "go-pain"),

    # AlternativeTo — "I want X but cheaper/different" = pain looking for solution.
    # No official RSS but their /software/X/ pages have alternatives + complaints.
    # Use Reddit /r/AlternativeTo as proxy (active community, RSS).
    ("alternativeto-reddit", "https://www.reddit.com/r/alternativeto/.rss",    "rss_atom", "en", "switching-pain"),

    # GitHub Issues by hot topic — actionable pain reports w/ context.
    # Issues label "bug" + recent = high-actionable pain density.
    ("gh-issues-llm",     "https://github.com/topics/llm-ops?utf8=%E2%9C%93&search=&type=&l=&q=",  "html", "en", "llm-ops-pain"),
    ("gh-issues-langchain", "https://github.com/langchain-ai/langchain/issues.atom",                "rss_atom", "en", "langchain-pain"),

    # Hacker News "Ask HN" tagged for pain — "what's missing", "why is X so hard"
    ("hn-ask",            "https://hn.algolia.com/api/v1/search?tags=ask_hn&query=missing+OR+broken&hitsPerPage=20", "json", "en", "ask-hn-pain"),
]


def _fetch(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None


def _parse_rss(content):
    """Generic RSS/Atom parser — returns list of {title, url, body}."""
    items = []
    if not content:
        return items
    # Try RSS <item>
    for m in re.finditer(
            r"<item[^>]*>\s*<title[^>]*>(?:<!\[CDATA\[)?([^<]+?)(?:\]\]>)?</title>"
            r".*?<link[^>]*>(?:<!\[CDATA\[)?([^<]+?)(?:\]\]>)?</link>"
            r"(?:.*?<description[^>]*>(?:<!\[CDATA\[)?([^<]+?)(?:\]\]>)?</description>)?",
            content, re.DOTALL | re.IGNORECASE):
        title = html.unescape(m.group(1).strip())[:200]
        url = m.group(2).strip()[:250]
        body = html.unescape(re.sub(r"<[^>]+>", " ", m.group(3) or ""))[:600]
        if len(title) > 10 and url.startswith("http"):
            items.append({"title": title, "url": url, "body": body})
        if len(items) >= 20:
            break
    if items:
        return items
    # Atom <entry>
    for m in re.finditer(
            r"<entry[^>]*>\s*<title[^>]*>([^<]+)</title>"
            r".*?<link[^>]*href=[\"']([^\"']+)[\"']"
            r"(?:.*?<summary[^>]*>([^<]+)</summary>)?",
            content, re.DOTALL | re.IGNORECASE):
        title = html.unescape(m.group(1).strip())[:200]
        url = m.group(2).strip()[:250]
        body = html.unescape(re.sub(r"<[^>]+>", " ", m.group(3) or ""))[:600]
        if len(title) > 10 and url.startswith("http"):
            items.append({"title": title, "url": url, "body": body})
        if len(items) >= 20:
            break
    return items


SEEN_FILE = Path("/opt/surrogate-1-harvest/state/multilang-pain-stream.seen.json")


def _load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def _save_seen(seen, max_keep=8000):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(seen) > max_keep:
        seen = set(list(seen)[-max_keep:])
    SEEN_FILE.write_text(json.dumps(list(seen)))


# Pain marker patterns — multilingual
PAIN_PATTERNS = re.compile(
    r"(how to|how do|why does|cant|can'?t|cannot|won'?t|"
    r"problem|issue|broken|bug|fail|error|stuck|slow|too expensive|"
    r"alternative|replacement|migrate from|switch from|"
    r"frustrating|annoying|hate|wish.*had|need.*help|"
    r"ทำไม|ปัญหา|ติด|แก้ไม่ได้|ช่วย|แนะนำ|"
    r"как|почему|проблема|не работает|"
    r"なぜ|どうやって|問題|不具合|エラー|"
    r"为什么|怎么|问题|错误|"
    r"왜|어떻게|문제|오류|"
    r"por que|como|problema|erro|"
    r"warum|wie|problem|fehler|"
    r"pourquoi|comment|problème|erreur|"
    r"por qué|cómo|problema|error)",
    re.IGNORECASE
)


def has_pain_signal(text):
    """Filter — keep only items with pain markers."""
    return bool(PAIN_PATTERNS.search(text))


def do_one():
    seen = _load_seen()
    new_items = 0
    # Random shuffle so we don't always start at top of list
    sources = SOURCES[:]
    random.shuffle(sources)

    for name, url, parser, lang, category in sources[:8]:  # 8 per cycle
        content = _fetch(url, timeout=10)
        if not content:
            continue
        for it in _parse_rss(content):
            sig = hashlib.sha256(it["url"].encode()).hexdigest()[:16]
            if sig in seen:
                continue
            text = f"{it['title']}\n\n{it['body']}"
            if not has_pain_signal(text):
                seen.add(sig)
                continue
            seen.add(sig)
            # Build research item
            item = new_item("null", "discovery", text)
            item["id"] = (datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S") +
                          f"-{lang}{name[:8]}-{sig}")
            item["source"] = {
                "type": "multilang-pain-stream",
                "feed": name,
                "url": it["url"],
                "lang": lang,
                "category": category,
            }
            item["current"]["text"] = (
                f"## {name} ({lang}) pain signal — {category}\n\n"
                f"**Title:** {it['title']}\n\n"
                f"**URL:** {it['url']}\n\n"
                f"{it['body']}\n"
            )
            try:
                write_item(item, "research")
                new_items += 1
            except Exception as e:
                log("multilang-pain", f"  ✗ write_item: {type(e).__name__}: {e}")

        time.sleep(2.0)  # gap between sources to avoid bursting

    _save_seen(seen)
    if new_items > 0:
        log("multilang-pain", f"+{new_items} pain signals from {len(sources[:8])} feeds")
        return True
    return False


if __name__ == "__main__":
    daemon_loop("multilang-pain", 90, do_one)  # poll every 90s
