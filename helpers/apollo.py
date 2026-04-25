#!/usr/bin/env python3
"""Apollo company + roster lookups.

Separate from emails.py because emails.py is the email-finder waterfall;
this module is for the company-level + roster-level Apollo calls used by
the discovery pipeline.

Subcommands:
  org    --domain <d>           → company metadata (size, industry, description)
  roster --domain <d> [--limit N] → people roster (default 10)
  by-address --address <a>      → orgs HQ'd at this street address

All output is JSON to stdout. Errors go to stderr; exit code 0 always
(so SKILL.md callers can json.loads stdout regardless of partial failure).
"""
import argparse
import json
import os
import subprocess
import sys
from urllib.request import Request, urlopen


def _load_env():
    """Load API keys from the first .env file found (same logic as emails.py)."""
    candidates = [
        os.path.expanduser("~/Documents/Claude/.env"),
        os.path.expanduser("~/.claude/.env"),
        os.path.expanduser("~/.env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    import glob
    candidates += glob.glob("/sessions/*/mnt/Documents/Claude/.env")
    env_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not env_path:
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_env()
APOLLO_KEY = os.environ.get("APOLLO_API_KEY", "")


def _curl_post(url, payload):
    """POST via curl (Apollo's WAF rejects urllib's default UA)."""
    if not APOLLO_KEY:
        return {"_error": "APOLLO_API_KEY not set"}
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", url,
             "-H", f"x-api-key: {APOLLO_KEY}",
             "-H", "Content-Type: application/json",
             "-H", "Cache-Control: no-cache",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(result.stdout) if result.stdout else {"_error": "empty response"}
    except Exception as e:
        return {"_error": str(e)}


def _curl_get(url, params=None):
    if not APOLLO_KEY:
        return {"_error": "APOLLO_API_KEY not set"}
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "GET", url,
             "-H", f"x-api-key: {APOLLO_KEY}",
             "-H", "Cache-Control: no-cache"],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(result.stdout) if result.stdout else {"_error": "empty response"}
    except Exception as e:
        return {"_error": str(e)}


def org_enrich(domain):
    """Return Apollo organization metadata for a domain."""
    r = _curl_get(
        "https://api.apollo.io/api/v1/organizations/enrich",
        {"domain": domain},
    )
    if "_error" in r:
        return r
    org = r.get("organization") or {}
    return {
        "domain": domain,
        "name": org.get("name"),
        "estimated_num_employees": org.get("estimated_num_employees"),
        "industry": org.get("industry"),
        "industries": org.get("industries", []),
        "short_description": org.get("short_description"),
        "website_url": org.get("website_url"),
        "primary_phone": (org.get("primary_phone") or {}).get("number"),
        "city": org.get("city"),
        "state": org.get("state"),
        "country": org.get("country"),
        "founded_year": org.get("founded_year"),
        "linkedin_url": org.get("linkedin_url"),
    }


LA_METRO_LOCATIONS = [
    "Los Angeles, California",
    "Beverly Hills, California",
    "Santa Monica, California",
    "Brentwood, California",
    "West Hollywood, California",
    "Westwood, California",
    "Culver City, California",
    "Pasadena, California",
    "Burbank, California",
    "Glendale, California",
    "Marina Del Rey, California",
    "Playa Vista, California",
    "El Segundo, California",
    "Manhattan Beach, California",
]


def roster(domain, limit=10, locations=None, prefer_la=True):
    """Return up to N people at a company.

    Two-pass strategy when prefer_la=True (default):
      1. Senior-tier roster filtered to locations= (or LA metro if not specified)
      2. If pass 1 empty, fall back to no-location-filter (with location field
         in result so caller can detect non-LA contacts)
      3. If both empty, fall back to domain-only no-seniority

    locations: list of "City, State" strings to filter by. Defaults to
      LA_METRO_LOCATIONS when prefer_la=True. Pass [] to disable filtering.

    Returns {domain, count, people, location_filter_used, no_la_head}.
    """
    if locations is None:
        locations = LA_METRO_LOCATIONS if prefer_la else []

    senior_seniorities = ["owner", "founder", "c_suite", "partner", "vp", "head", "director"]

    def _query(loc_filter, seniorities):
        payload = {
            "q_organization_domains_list": [domain],
            "per_page": limit,
            "page": 1,
        }
        if seniorities:
            payload["person_seniorities"] = seniorities
        if loc_filter:
            payload["person_locations"] = loc_filter
        return _curl_post(
            "https://api.apollo.io/api/v1/mixed_people/api_search",
            payload,
        )

    no_la_head = False
    location_filter_used = "la_metro" if locations else "none"

    # Pass 1: location-filtered senior roster
    if locations:
        r = _query(locations, senior_seniorities)
        if "_error" in r:
            return {"_error": r["_error"], "people": [], "no_la_head": False,
                    "location_filter_used": location_filter_used}
        people = r.get("people", []) or []
    else:
        people = []

    # Pass 2: drop location filter if pass 1 empty
    if not people and locations:
        r = _query([], senior_seniorities)
        if "_error" not in r:
            people = r.get("people", []) or []
        no_la_head = True
        location_filter_used = "none_after_la_empty"

    # Pass 3: drop seniority filter if still empty
    if not people:
        r = _query([], [])
        if "_error" not in r:
            people = r.get("people", []) or []
    # Slim each person down to fields the stakeholder picker needs
    slim = []
    for p in people[:limit]:
        slim.append({
            "id": p.get("id"),
            "first_name": p.get("first_name", ""),
            "last_name": p.get("last_name", ""),
            "title": p.get("title", ""),
            "city": p.get("city", ""),
            "state": p.get("state", ""),
            "linkedin_url": p.get("linkedin_url", ""),
            "seniority": p.get("seniority", ""),
            "departments": p.get("departments", []),
        })
    return {
        "domain": domain,
        "count": len(slim),
        "people": slim,
        "no_la_head": no_la_head,
        "location_filter_used": location_filter_used,
    }


def enrich_person(apollo_id):
    """Unlock full person profile by Apollo ID.

    The roster endpoint (mixed_people/api_search) returns first_name +
    obfuscated last_name + has_email flag. This endpoint spends 1 Apollo
    credit to return the unredacted person + verified email.

    Use after the SKILL has picked stakeholders by index — only enrich the
    1-2 chosen, never the whole roster.
    """
    if not apollo_id:
        return {"_error": "missing apollo_id"}
    # Apollo /v1/people/{id} returns full person record
    r = _curl_get(f"https://api.apollo.io/api/v1/people/{apollo_id}")
    if "_error" in r:
        return r
    p = r.get("person") or r  # endpoint sometimes returns person directly
    return {
        "apollo_id": apollo_id,
        "first_name": p.get("first_name"),
        "last_name": p.get("last_name"),
        "name": p.get("name"),
        "title": p.get("title"),
        "city": p.get("city"),
        "state": p.get("state"),
        "country": p.get("country"),
        "email": p.get("email"),
        "linkedin_url": p.get("linkedin_url"),
        "organization": (p.get("organization") or {}).get("name"),
        "domain": (p.get("organization") or {}).get("primary_domain"),
    }


def orgs_at_address(address):
    """Find orgs HQ'd at a specific street address.

    Apollo doesn't have a clean "search by street address" — we use
    keyword tags + city filter, then post-filter by street-number match.
    Useful as an extra signal alongside LA City data.
    """
    main_addr = address.split(",")[0].strip().lower()
    street_num = main_addr.split()[0] if main_addr else ""
    # Detect city from address for the location filter
    parts = [p.strip() for p in address.split(",")]
    city_state = None
    if len(parts) >= 3:
        city_state = f"{parts[1]}, {parts[2].split()[0] if parts[2] else 'California'}"
    elif len(parts) == 2:
        city_state = parts[1]

    payload = {
        "q_organization_keyword_tags": [main_addr],
        "per_page": 25,
    }
    if city_state:
        payload["organization_locations"] = [city_state]

    r = _curl_post(
        "https://api.apollo.io/api/v1/mixed_companies/search?reveal_phone_number=false",
        payload,
    )
    if "_error" in r:
        return {"_error": r["_error"], "orgs": []}
    orgs = r.get("organizations", []) or []
    matched = []
    for o in orgs:
        street = (o.get("street_address") or "").lower()
        if street_num and street_num in street:
            domain_raw = (o.get("website_url") or "").replace("http://", "").replace("https://", "").split("/")[0]
            matched.append({
                "company": o.get("name", "")[:120],
                "suite": None,  # Apollo rarely surfaces suite
                "domain": domain_raw,
                "confidence": 0.7,
                "evidence": f"Apollo HQ: {street}",
                "source": "apollo_address",
                "source_url": o.get("website_url", ""),
                "estimated_num_employees": o.get("estimated_num_employees"),
                "industry": o.get("industry"),
            })
    return {"address": address, "count": len(matched), "orgs": matched}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_org = sub.add_parser("org", help="Get company metadata for a domain")
    p_org.add_argument("--domain", required=True)

    p_roster = sub.add_parser("roster", help="Get people roster for a domain")
    p_roster.add_argument("--domain", required=True)
    p_roster.add_argument("--limit", type=int, default=10)
    p_roster.add_argument("--locations", nargs="*", default=None,
                          help='Override location filter. Pass empty string for no filter, '
                               'or city,state strings (e.g. "Los Angeles, California"). '
                               'Defaults to LA metro.')
    p_roster.add_argument("--no-la", action="store_true",
                          help="Disable the LA-metro default filter (return all locations).")

    p_addr = sub.add_parser("by-address", help="Find orgs HQ'd at this address")
    p_addr.add_argument("--address", required=True)

    p_enrich = sub.add_parser("enrich", help="Unlock full person record by Apollo ID")
    p_enrich.add_argument("--id", required=True, dest="apollo_id")

    args = ap.parse_args()

    if not APOLLO_KEY:
        print("WARNING: APOLLO_API_KEY not set", file=sys.stderr)

    if args.cmd == "org":
        result = org_enrich(args.domain)
    elif args.cmd == "roster":
        # Resolve locations argument
        locs = None
        if args.no_la:
            locs = []
        elif args.locations is not None:
            locs = [l for l in args.locations if l]  # drop empty strings
        result = roster(args.domain, args.limit, locations=locs)
    elif args.cmd == "by-address":
        result = orgs_at_address(args.address)
    elif args.cmd == "enrich":
        result = enrich_person(args.apollo_id)
    else:
        result = {"_error": f"unknown cmd {args.cmd}"}

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
