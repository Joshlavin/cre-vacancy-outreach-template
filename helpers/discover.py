#!/usr/bin/env python3
"""Tenant discovery + proximity ranking for Kurt CRE outreach.

Usage:
  python3 discover.py "1880 Century Park E, Los Angeles, CA 90067" --suite 412 --top 15

Prints JSON to stdout:
  {
    "vacancy": {...},
    "building_format": 3 or 4,
    "ranked_tenants": [
      {"company": "...", "suite": "...", "relation": "...", "proximity_score": 50, "domain": null}
    ],
    "biz_mgr_hosts": {
      "1600": {"count": 357, "likely_host": "Gelfand Rennert & Feldman"}
    },
    "outgoing_tenants": ["Curewell Capital Management"],
    "stats": {"raw": 847, "unique": 496, "excluded": 14, "mail_drop": 407}
  }
"""
import argparse
import json
import re
import sys
from collections import Counter
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import URLError


# ============ ADDRESS NORMALIZER ============
_STREET_TYPE_MAP = {
    "st": "street", "ave": "avenue", "av": "avenue", "blvd": "boulevard",
    "rd": "road", "dr": "drive", "pl": "place", "ln": "lane", "ct": "court",
    "pkwy": "parkway", "hwy": "highway",
}
_DIRECTION_MAP = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
}


def _expand(s):
    w = s.replace(".", "").lower()
    out = {w}
    for mapping in (_STREET_TYPE_MAP, _DIRECTION_MAP):
        if w in mapping:
            out.add(mapping[w])
        for k, v in mapping.items():
            if w == v:
                out.add(k)
    return out


def parse_address(address):
    """Parse 'NUM DIR STREET TYPE, CITY, STATE ZIP' into components + variants."""
    raw = address.strip()
    main = raw.split(",")[0].strip()
    rest = ",".join(raw.split(",")[1:]).strip()
    m = re.match(
        r"^\s*(\d+[A-Za-z]?)\s+(?:([NSEWnsew]{1,2})\s+)?(.+?)\s+([A-Za-z]+)\s*$",
        main,
    )
    if not m:
        num_m = re.match(r"^\s*(\d+)\s+(.*)$", main)
        return {
            "raw": raw, "number": num_m.group(1) if num_m else "",
            "street": (num_m.group(2) if num_m else main).strip(),
            "short": main, "variants": [main.upper()],
        }
    number, direction, street, street_type = m.groups()
    direction = (direction or "").lower()

    dir_variants = _expand(direction) if direction else {""}
    type_variants = _expand(street_type) if street_type else {""}
    variants = set()
    for d in dir_variants:
        for t in type_variants:
            d_str = f" {d}" if d else ""
            t_str = f" {t}" if t else ""
            variants.add(f"{number}{d_str} {street}{t_str}".upper().strip())
    for d in dir_variants:
        d_str = f" {d}" if d else ""
        variants.add(f"{number}{d_str} {street}".upper().strip())

    return {
        "raw": raw, "number": number, "direction": direction,
        "street": street.strip(), "street_type": street_type.strip(),
        "short": f"{number}{(' ' + direction.upper()) if direction else ''} {street}".strip(),
        "variants": sorted(variants),
    }


# ============ LA CITY API ============
def _fetch_json(url, params=None, timeout=30):
    full = url + "?" + urlencode(params) if params else url
    try:
        req = Request(full, headers={"User-Agent": "kurt-outreach-skill/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (URLError, TimeoutError) as e:
        return {"_error": str(e)}


def _extract_suite(s):
    if not s: return None
    m = re.search(r"(?:#|\bSTE\.?\b|\bSUITE\b|\bUNIT\b)\s*([\w\-]+)", s, re.I)
    return m.group(1) if m else None


def la_city_tenants(parsed, years_back=3):
    """Paginated Socrata query on LA City business licenses.

    years_back: only include licenses active within this many years (reduces stale data).
    """
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=365 * years_back)).strftime("%Y-%m-%dT00:00:00.000")
    url = "https://data.lacity.org/resource/6rrh-rzua.json"
    seen = set()
    out = []
    for variant in parsed["variants"]:
        offset = 0
        while True:
            where = (
                f"starts_with(upper(street_address), upper('{variant}'))"
                f" AND location_start_date >= '{cutoff}'"
            )
            params = {
                "$where": where,
                "$limit": "1000", "$offset": str(offset),
            }
            data = _fetch_json(url, params)
            if isinstance(data, dict) and "_error" in data:
                break
            if not isinstance(data, list) or not data:
                break
            for row in data:
                acct = row.get("location_account")
                if acct and acct in seen: continue
                if acct: seen.add(acct)
                name = row.get("dba_name") or row.get("business_name") or ""
                addr = row.get("street_address", "")
                suite = _extract_suite(addr)
                if not name: continue
                out.append({
                    "company": name.title(),
                    "suite": suite,
                    "raw_address": addr,
                    "naics": (row.get("primary_naics_description") or row.get("naics") or "").strip(),
                    "license_start": (row.get("location_start_date") or "")[:10],
                })
            if len(data) < 1000: break
            offset += 1000
    return out


# ============ FILTERS ============
EXCLUSION_PATTERNS = [
    # Coworking
    r"\bindustrious\b", r"\bwework\b", r"\bregus\b", r"\bconvene\b", r"\bknotel\b",
    # Brokerages
    r"\bcbre\b", r"\bcushman\b", r"\bjll\b", r"\bcolliers\b", r"\bkidder mathews\b",
    r"\bavison young\b", r"\bcharles dunn\b", r"\bmadison partners\b",
    r"\bla realty partners\b", r"\bnewmark\b", r"\btranswestern\b", r"\bsavills\b",
    # Landlords / developers
    r"\bhudson pacific\b", r"\bbrookfield\b", r"\bonni\b", r"\bhines\b",
    r"\brelated companies\b", r"\bboston properties\b", r"\bdouglas emmett\b",
]


def is_excluded(company):
    c = (company or "").lower()
    return any(re.search(p, c) for p in EXCLUSION_PATTERNS)


_BIZ_MGR_RESCUE = [
    r"\b(cpa|cpas|accountancy|accountants|llp|pllc)\b",
    r"\b(business management|advisors|advisory)\b",
    r"&\s*(co|company|feldman|associates|partners)\b",
    r"\b(gelfand|kern|nksfb|miller kaplan|warner lusitano)\b",
]


def looks_like_biz_mgr(name):
    n = (name or "").lower()
    return any(re.search(p, n) for p in _BIZ_MGR_RESCUE)


def flag_mail_drops(tenants, threshold=8, min_industries=5):
    """Flag suites that are registered-agent / virtual-mailbox hosts. Rescue the actual biz-mgr firm."""
    by_suite = {}
    for t in tenants:
        s = t.get("suite")
        if s: by_suite.setdefault(s, []).append(t)
    mail_drop_suites = set()
    host_info = {}
    for suite, entries in by_suite.items():
        if len(entries) < threshold: continue
        naics = {(e.get("naics") or "")[:20].lower().strip() for e in entries if e.get("naics")}
        if len(naics) >= min_industries:
            mail_drop_suites.add(suite)
            hosts = [e for e in entries if looks_like_biz_mgr(e.get("company", ""))]
            if hosts:
                best_host = sorted(hosts, key=lambda e: -len(e.get("company", "")))[0]
                host_info[suite] = {"count": len(entries), "likely_host": best_host["company"]}
            else:
                host_info[suite] = {"count": len(entries), "likely_host": "(unknown)"}
    for t in tenants:
        if t.get("suite") in mail_drop_suites:
            t["mail_drop"] = not looks_like_biz_mgr(t.get("company", ""))
    return tenants, host_info


# ============ SUITE / PROXIMITY ============
def parse_suite(suite):
    if not suite: return (None, None)
    s = str(suite).strip().upper()
    m = re.match(r"^(\d+)[- ]?([A-Z]?)$", s)
    if m: return (int(m.group(1)), m.group(2) or None)
    m2 = re.match(r"^(\d+)(?:ST|ND|RD|TH|FL)$", s)
    if m2: return (int(m2.group(1)) * 100, "FL")
    m3 = re.match(r"^(?:MEZZ|PH|P|LL|B)\s*(\d+)([A-Z]?)$", s)
    if m3: return (int(m3.group(1)), "P")
    m4 = re.match(r"^(\d+)", s)
    if m4: return (int(m4.group(1)), None)
    return (None, None)


def detect_building_format(tenants):
    c = Counter()
    for t in tenants:
        n, _ = parse_suite(t.get("suite"))
        if n is None: continue
        if 100 <= n < 1000: c[3] += 1
        elif 1000 <= n < 10000: c[4] += 1
    total = c[3] + c[4]
    if total == 0: return 3
    return 4 if c[4] / total > 0.5 else 3


def suite_to_floor(n, fmt):
    if n is None: return None
    return n // 100


def score_proximity(tenant_suite, vacancy_suite, fmt):
    t, _ = parse_suite(tenant_suite)
    v, _ = parse_suite(vacancy_suite)
    if t is None or v is None: return (0, "unknown")
    if t == v: return (-1, "same_suite_outgoing")
    tf = suite_to_floor(t, fmt)
    vf = suite_to_floor(v, fmt)
    if tf == vf:
        diff = abs(t - v)
        if diff <= 5: return (50, "right next door")
        if diff <= 20: return (40, "adjacent same floor")
        return (30, "same floor")
    d = abs(tf - vf)
    if d == 1: return (15, "floor above" if tf > vf else "floor below")
    if d == 2: return (8, "two floors away")
    if d <= 5: return (3, "nearby floor")
    return (1, "same building")


# ============ MAIN ============
def run(address, suite, top_n=15):
    parsed = parse_address(address)
    tenants = la_city_tenants(parsed)

    # Dedupe by company+suite
    by_key = {}
    for t in tenants:
        name_norm = re.sub(r"\s+(inc\.?|llc|llp|pc|pllc|corp\.?|co\.?|ltd\.?)$", "", t["company"].lower()).strip()
        key = (name_norm, t.get("suite") or "")
        if key not in by_key:
            by_key[key] = t.copy()
    merged = list(by_key.values())

    # Exclude landlords/coworking/brokers
    for t in merged:
        t["excluded"] = is_excluded(t["company"])

    # Mail-drop detection
    merged, biz_mgr_hosts = flag_mail_drops(merged)

    # Rank by proximity
    fmt = detect_building_format(merged)
    ranked = []
    outgoing = []
    for t in merged:
        if t.get("excluded") or t.get("mail_drop"):
            continue
        score, label = score_proximity(t.get("suite"), suite, fmt)
        if score < 0:
            outgoing.append(t["company"])
            continue
        # Only include same-floor and immediately adjacent floors (score >= 15).
        # "Same building" (score 1-8) is too weak a hook for the outreach pitch.
        if score < 15:
            continue
        ranked.append({
            "company": t["company"],
            "suite": t.get("suite"),
            "relation": label,
            "proximity_score": score,
            "domain": None,  # sub-agent will resolve
            "naics": t.get("naics", ""),
            "mail_drop_host": looks_like_biz_mgr(t["company"]) and t.get("suite") in biz_mgr_hosts,
        })
    ranked.sort(key=lambda x: -x["proximity_score"])

    return {
        "vacancy": {"address": address, "parsed": parsed, "suite": suite},
        "building_format": fmt,
        "ranked_tenants": ranked[:top_n],
        "biz_mgr_hosts": biz_mgr_hosts,
        "outgoing_tenants": outgoing,
        "stats": {
            "raw": len(tenants), "unique": len(merged),
            "excluded": sum(1 for t in merged if t.get("excluded")),
            "mail_drop": sum(1 for t in merged if t.get("mail_drop")),
            "considered": len(ranked),
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address")
    p.add_argument("--suite", required=True)
    p.add_argument("--top", type=int, default=15)
    args = p.parse_args()
    result = run(args.address, args.suite, args.top)
    print(json.dumps(result, indent=2, default=str))
