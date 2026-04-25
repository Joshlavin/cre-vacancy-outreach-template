#!/usr/bin/env python3
"""Search-result cache for sweep.py.

Why: Firecrawl search results are non-deterministic across calls — the same
query returns slightly different rankings each time. This breaks reproducibility
("the cron found IEQ Capital yesterday, why isn't it there today?") and burns
credits re-querying the same (suite, address) pairs.

The cache stores raw Firecrawl results keyed by SHA1(query). 30-day TTL.

Usage from Python:
  from helpers.search_cache import cached_search
  results = cached_search(query, fetch_fn, ttl_days=30)
  # fetch_fn is called only on cache miss

CLI:
  python3 search_cache.py stats
  python3 search_cache.py prune --days 30
  python3 search_cache.py clear
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path


def _cache_dir():
    """Resolve cache/searches relative to the repo root.

    Search-result cache is intentionally NOT committed back to the repo
    (the .gitignore excludes cache/searches/) — it's regenerable and can
    grow large. Within a single run, however, we want sweep + emails
    helpers to share a cache, so we anchor it on the repo working dir.
    """
    here = Path(__file__).resolve().parent
    repo_root = here.parent  # helpers/ is one level under repo root
    target = repo_root / "cache" / "searches"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _key_path(query):
    h = hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
    return _cache_dir() / f"{h}.json"


def cached_search(query, fetch_fn, ttl_days=30, force_refresh=False):
    """Return cached results for query, or call fetch_fn(query) on miss."""
    p = _key_path(query)
    if p.exists() and not force_refresh:
        try:
            data = json.loads(p.read_text())
            cached_at = dt.datetime.fromisoformat(data["cached_at"].rstrip("Z"))
            age_days = (dt.datetime.utcnow() - cached_at).days
            if age_days < ttl_days:
                return data["results"]
        except (KeyError, ValueError, json.JSONDecodeError):
            pass  # fall through to refetch

    results = fetch_fn(query)
    try:
        p.write_text(json.dumps({
            "query": query,
            "cached_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "results": results,
        }, default=str))
    except Exception:
        pass  # cache write failure shouldn't break the call
    return results


def stats():
    d = _cache_dir()
    files = list(d.glob("*.json"))
    total_size = sum(f.stat().st_size for f in files)
    return {
        "cache_dir": str(d),
        "entries": len(files),
        "total_size_kb": round(total_size / 1024, 1),
    }


def prune(ttl_days=30):
    d = _cache_dir()
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=ttl_days)
    pruned = 0
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            cached_at = dt.datetime.fromisoformat(data["cached_at"].rstrip("Z"))
            if cached_at < cutoff:
                f.unlink()
                pruned += 1
        except Exception:
            f.unlink()  # corrupt cache entry — drop
            pruned += 1
    return {"pruned": pruned, "remaining": len(list(d.glob("*.json")))}


def clear():
    d = _cache_dir()
    n = 0
    for f in d.glob("*.json"):
        f.unlink()
        n += 1
    return {"cleared": n}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--days", type=int, default=30)
    sub.add_parser("clear")
    args = ap.parse_args()
    if args.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.cmd == "prune":
        print(json.dumps(prune(args.days), indent=2))
    elif args.cmd == "clear":
        print(json.dumps(clear(), indent=2))


if __name__ == "__main__":
    main()
