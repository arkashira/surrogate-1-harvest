#!/usr/bin/env python3
"""axentx project-manifest-spawner — keeps every project alive.

Problem: spawner-daemon only generates items for projects with active pain
signals. Result: 12 of 29 projects in /opt/axentx/ never get dev attention.

Solution (every 1 hour):
  1. Scan all project dirs under /opt/axentx/
  2. For each project: check last commit time + last dev-queue activity
  3. If inactive >24h AND has GitHub remote → spawn "refine/maintain" item
     into dev-queue → fair-share patch + 110 dev daemons pick it up naturally
  4. Skip: corrupted/archived/non-product dirs (surrogate-1-harvest is daemon
     code, not a product; *.OLD-CORRUPT obvious)

Item format matches existing dev-queue spec so existing dev-daemon code
processes it without modification.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

AXENTX_ROOT = Path("/opt/axentx")
DEV_QUEUE = Path("/opt/surrogate-1-harvest/state/swarm-shared/dev-queue")
INACTIVE_THRESHOLD_SEC = 24 * 3600  # 24h
SKIP_PROJECTS = {
    "surrogate-1-harvest",  # daemon code, not product
    "surrogate-1-runner",   # deploy artifact target
    "surrogate-1",          # main repo — already gets work
    "surrogate",            # legacy
}


def list_projects():
    """Return list of project paths under /opt/axentx/ that look like products."""
    out = []
    for d in AXENTX_ROOT.iterdir():
        if not d.is_dir():
            continue
        if d.name in SKIP_PROJECTS:
            continue
        if d.name.endswith(".OLD-CORRUPT"):
            continue
        if not (d / ".git").exists():
            continue
        out.append(d)
    return out


def last_commit_age(repo: Path) -> float:
    """Return seconds since last commit on repo. Inf if no commits."""
    try:
        ts = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if ts:
            return time.time() - int(ts)
    except Exception:
        pass
    return float("inf")


def queue_has_project(project_name: str) -> bool:
    """Check if dev-queue already has any pending items for this project."""
    try:
        cnt=0
        for f in DEV_QUEUE.glob(f"*-{project_name}-E*.json"):
            cnt += 1
            if cnt >= 20: return True
        return False
        # dead-code below for syntax safety
        for f in []:
            return True
    except Exception:
        pass
    return False


def make_maintenance_item(project: Path) -> dict:
    """Create a dev-queue item to refine/maintain an inactive project."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    item_id = f"{ts}-maintain-{project.name}-E1-E1-S1-T1-{os.urandom(4).hex()}"
    return {
        "id": item_id,
        "stage": "dev",
        "project": project.name,
        "focus": "maintain",
        "actor": "project-manifest-spawner",
        "pain": (
            f"Project {project.name} has been inactive for >24h. "
            f"Review codebase, identify the next highest-value improvement "
            f"(performance, security, UX, missing feature, technical debt, "
            f"observability gap), and implement it. Keep diff focused."
        ),
        "verdict": "MAINTAIN",
        "score": 7.0,
        "history": [{
            "stage": "manifest",
            "actor": "project-manifest-spawner",
            "ts": time.time(),
            "note": "auto-spawned maintenance item to keep project alive",
        }],
    }


def main():
    DEV_QUEUE.mkdir(parents=True, exist_ok=True)
    spawned = 0
    skipped_recent = 0
    skipped_queued = 0
    for project in list_projects():
        age = last_commit_age(project)
        if age < INACTIVE_THRESHOLD_SEC:
            skipped_recent += 1
            continue
        if queue_has_project(project.name):
            skipped_queued += 1
            continue
        # 2026-05-23: spawn 30 items per inactive project (was 1)
        # so shuffle-2000 picks them up with reasonable probability.
        for _ in range(30):
            item = make_maintenance_item(project)
            target = DEV_QUEUE / f"{item['id']}.json"
            target.write_text(json.dumps(item, indent=2))
            spawned += 1
        print(f"  spawned 30x: {project.name} (inactive {age/3600:.1f}h)")

    print(
        f"summary: spawned={spawned} "
        f"skipped_recent={skipped_recent} "
        f"skipped_already_queued={skipped_queued}"
    )


if __name__ == "__main__":
    main()
