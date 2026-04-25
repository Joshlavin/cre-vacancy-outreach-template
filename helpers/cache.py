#!/usr/bin/env python3
"""Building tenant cache.

Stores verified tenant lists per (building_address, floor_range) with timestamps.
On a subsequent vacancy at the same building within `ttl_days`, the cached
neighbor list is returned instead of re-running the research playbook —
saves the 60-90% of run tokens that go to research.

Usage:

  # Check cache (returns cached entry as JSON, or {} if miss/stale)
  python3 cache.py get --building "11601 Wilshire Blvd, Los Angeles, CA 90025" --floors 3,5

  # Write cache (stdin = JSON tenant list)
  echo '[{"company":"EPAM","suite":"350",...}]' | python3 cache.py put \\
    --building "11601 Wilshire Blvd, Los Angeles, CA 90025" --floors 3,5

  # List all cached buildings
  python3 cache.py list

  # Drop a single entry (force re-research next time)
  python3 cache.py drop --building "11601 Wilshire Blvd, Los Angeles, CA 90025" --floors 3,5

Storage: cache/buildings/<sha1>.json (relative to repo root). The Routine's
post-run hook commits this directory back to GitHub each night, so the
60-day TTL persists across runs.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

DEFAULT_TTL_DAYS = 60


def _cache_dir():
    """Resolve cache/buildings relative to the repo root.

    The Routine clones the repo each run into a working directory;
    cache/buildings is committed back via the post-run hook so the
    60-day TTL persists across runs. We anchor on the helpers/ folder
    location so the path is correct regardless of where Bash cwd is.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)  # helpers/ is one level under repo root
    target = os.path.join(repo_root, "cache", "buildings")
    os.makedirs(target, exist_ok=True)
    return target


def _normalize_address(addr):
    """Lowercase, strip punctuation/whitespace so '11601 Wilshire Blvd, LA, CA 90025'
    and '11601 wilshire blvd los angeles ca 90025' hash to the same key."""
    a = addr.lower()
    a = re.sub(r"[^a-z0-9]+", " ", a)
    a = re.sub(r"\s+", " ", a).strip()
    return a


def _key(building, floors):
    """floors is a tuple/list like [3, 5] meaning floors 3 through 5 inclusive."""
    norm = _normalize_address(building)
    floor_str = f"{min(floors)}-{max(floors)}"
    digest = hashlib.sha1(f"{norm}|{floor_str}".encode()).hexdigest()[:16]
    return digest


def _path(building, floors):
    return os.path.join(_cache_dir(), f"{_key(building, floors)}.json")


def _parse_floors(s):
    """Accept '3,5' or '3-5' or single '4'."""
    s = s.strip()
    if "," in s:
        parts = [int(p.strip()) for p in s.split(",")]
    elif "-" in s:
        parts = [int(p.strip()) for p in s.split("-")]
    else:
        parts = [int(s)]
    if len(parts) == 1:
        return [parts[0], parts[0]]
    return [min(parts), max(parts)]


def cmd_get(building, floors, ttl_days):
    p = _path(building, floors)
    if not os.path.isfile(p):
        return {}
    with open(p) as f:
        entry = json.load(f)
    cached_at = entry.get("cached_at_unix", 0)
    age_days = (time.time() - cached_at) / 86400
    if age_days > ttl_days:
        return {"_stale": True, "_age_days": round(age_days, 1)}
    entry["_age_days"] = round(age_days, 1)
    return entry


def cmd_put(building, floors, tenants):
    entry = {
        "building": building,
        "floors": list(floors),
        "tenants": tenants,
        "cached_at_unix": time.time(),
        "cached_at_iso": datetime.now(timezone.utc).isoformat(),
    }
    p = _path(building, floors)
    with open(p, "w") as f:
        json.dump(entry, f, indent=2)
    return {"_written": p, "tenant_count": len(tenants)}


def cmd_list():
    d = _cache_dir()
    out = []
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(d, fname)) as f:
            entry = json.load(f)
        age = round((time.time() - entry.get("cached_at_unix", 0)) / 86400, 1)
        out.append({
            "key": fname.replace(".json", ""),
            "building": entry.get("building"),
            "floors": entry.get("floors"),
            "tenant_count": len(entry.get("tenants", [])),
            "age_days": age,
        })
    return out


def cmd_drop(building, floors):
    p = _path(building, floors)
    if os.path.isfile(p):
        os.remove(p)
        return {"_dropped": p}
    return {"_not_found": p}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get")
    g.add_argument("--building", required=True)
    g.add_argument("--floors", required=True, help="e.g. 3,5 or 3-5 or 4")
    g.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)

    p = sub.add_parser("put")
    p.add_argument("--building", required=True)
    p.add_argument("--floors", required=True)

    sub.add_parser("list")

    d = sub.add_parser("drop")
    d.add_argument("--building", required=True)
    d.add_argument("--floors", required=True)

    args = parser.parse_args()

    if args.cmd == "get":
        print(json.dumps(cmd_get(args.building, _parse_floors(args.floors), args.ttl_days), indent=2))
    elif args.cmd == "put":
        tenants = json.loads(sys.stdin.read())
        if not isinstance(tenants, list):
            sys.exit("put: stdin must be a JSON array of tenant dicts")
        print(json.dumps(cmd_put(args.building, _parse_floors(args.floors), tenants), indent=2))
    elif args.cmd == "list":
        print(json.dumps(cmd_list(), indent=2))
    elif args.cmd == "drop":
        print(json.dumps(cmd_drop(args.building, _parse_floors(args.floors)), indent=2))
