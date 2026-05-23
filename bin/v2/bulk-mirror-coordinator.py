"""Surrogate-1 v2 — Bulk mirror coordinator with claim queue.

User feedback 2026-04-29:
  "ทุก agent ทำงานร่วมกัน และไม่ไปที่ซ้ำๆ หาจาก keyword แล้วไปที่ใหม่ๆ"

This script is the work-claim broker: any number of mirror/discoverer/enricher
agents can pull tasks from here. Each task = (dataset_repo, expected_size,
priority). Claims persist in the central SQLite dedup store (already used by
DedupStore for content dedup). Each claim has lease (15 min). Crashes auto-
expire so other workers pick up.

Usage from agents:
  python3 bulk-mirror-coordinator.py claim          # → prints next task
  python3 bulk-mirror-coordinator.py done <id>      # mark done
  python3 bulk-mirror-coordinator.py status         # show queue + claimed
  python3 bulk-mirror-coordinator.py seed           # one-time seed from massive list
"""
import os, sys, sqlite3, time, json
from pathlib import Path

DB_PATH = Path.home() / ".surrogate/state/bulk-mirror-claims.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
# Two registries: bulk-datasets-massive.txt (legacy 4-col) +
# trillion-token-sources.txt (5-col with streaming flag). Seed reads both.
LIST_PATHS = [
    Path.home() / ".surrogate/bin/v2/bulk-datasets-massive.txt",
    Path.home() / ".surrogate/bin/v2/trillion-token-sources.txt",
]
LEASE_SECS = 15 * 60   # claim expires after 15 min if no `done` call


def db():
    c = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_id TEXT UNIQUE,
        category TEXT,
        max_samples INTEGER,
        priority INTEGER,
        status TEXT DEFAULT 'pending',  -- pending / claimed / done / failed
        worker_id TEXT,
        claimed_at INTEGER,
        completed_at INTEGER,
        kept_count INTEGER DEFAULT 0,
        error TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_claims_status_pri ON claims(status, priority)")
    return c


def seed():
    """Load both massive + trillion-token registries into queue."""
    c = db()
    n_total = 0
    for list_path in LIST_PATHS:
        if not list_path.exists():
            print(f"  skip (missing): {list_path}")
            continue
        n = 0
        with open(list_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    parts = line.split("|")
                    # Accept 4-col (legacy) or 5-col (trillion-tokens with streaming flag)
                    repo, cat, mx, pri = parts[0], parts[1], parts[2], parts[3]
                    c.execute("""INSERT OR IGNORE INTO claims
                                 (repo_id, category, max_samples, priority)
                                 VALUES (?, ?, ?, ?)""",
                              (repo.strip(), cat.strip(), int(mx), int(pri)))
                    if c.total_changes:
                        n += 1
                except Exception as e:
                    print(f"  skip {line[:60]}: {e}")
        print(f"  seeded {n} from {list_path.name}")
        n_total += n
    c.close()
    print(f"✅ total seeded {n_total} new entries (existing rows untouched)")


def claim(worker_id: str | None = None):
    """Atomically claim next pending task by priority."""
    worker_id = worker_id or f"w-{os.getpid()}-{int(time.time())}"
    c = db()
    now = int(time.time())
    # Expire stale claims first
    c.execute("""UPDATE claims SET status='pending', worker_id=NULL
                 WHERE status='claimed' AND claimed_at < ?""",
              (now - LEASE_SECS,))
    # Claim next pending in priority order
    c.execute("""UPDATE claims
                 SET status='claimed', worker_id=?, claimed_at=?
                 WHERE id = (
                     SELECT id FROM claims
                     WHERE status='pending'
                     ORDER BY priority ASC, RANDOM()
                     LIMIT 1
                 )
                 RETURNING id, repo_id, category, max_samples, priority""",
              (worker_id, now))
    row = c.fetchone()
    c.close()
    if row:
        cid, repo, cat, mx, pri = row
        print(json.dumps({"id": cid, "repo_id": repo, "category": cat,
                          "max_samples": mx, "priority": pri,
                          "worker_id": worker_id}))
    else:
        print(json.dumps({"id": None, "msg": "no pending tasks"}))


def done(claim_id: int, kept: int = 0, error: str | None = None):
    c = db()
    status = "failed" if error else "done"
    c.execute("""UPDATE claims SET status=?, completed_at=?, kept_count=?, error=?
                 WHERE id=?""",
              (status, int(time.time()), kept, error, claim_id))
    c.close()
    print(json.dumps({"id": claim_id, "status": status, "kept": kept}))


def status():
    c = db()
    cur = c.execute("""SELECT status, COUNT(*), SUM(kept_count)
                       FROM claims GROUP BY status""")
    print(f"{'status':<12} {'count':>6} {'kept_sum':>12}")
    for s, n, k in cur:
        print(f"{s:<12} {n:>6} {k or 0:>12}")
    print()
    cur = c.execute("""SELECT repo_id, status, kept_count, worker_id
                       FROM claims
                       WHERE status='claimed' OR status='failed'
                       ORDER BY claimed_at DESC LIMIT 20""")
    print(f"{'repo':<55} {'status':<10} {'kept':>8} {'worker':<20}")
    for repo, s, k, w in cur:
        print(f"{repo[:55]:<55} {s:<10} {k or 0:>8} {w or '-':<20}")
    c.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "seed":
        seed()
    elif cmd == "claim":
        worker_id = sys.argv[2] if len(sys.argv) > 2 else None
        claim(worker_id)
    elif cmd == "done":
        cid = int(sys.argv[2])
        kept = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        err = sys.argv[4] if len(sys.argv) > 4 else None
        done(cid, kept, err)
    elif cmd == "status":
        status()
    else:
        print(f"unknown: {cmd}", file=sys.stderr)
        sys.exit(1)
