#!/usr/bin/env python3
"""Batch email finder.

Waterfall:
  1. Apollo people-match (primary — 90%+ of hits)
  2. Firecrawl scrape of company team/about/contact pages (catches small firms
     that publish individual emails on their site)
  3. Pattern guess (returned with method "pattern_guess" so caller knows it's unverified)
  4. Return null + method "not_found"

Usage:
  python3 emails.py --names-json '[{"first":"Brent","last":"Wagner","domain":"acorecapital.com"}]'

Optional fields per contact:
  - "website_url": Direct URL to scrape (team page, about page, etc.)
"""
import argparse
import json
import os
import re
import subprocess
import sys
from urllib.request import Request, urlopen


def _load_env():
    """Load API keys from the first .env file found across standard locations.

    Search order (first match wins):
      1. ~/Documents/Claude/.env   — default install location
      2. ~/.claude/.env            — Claude Code standard location
      3. ~/.env                    — common fallback
      4. .env next to this script  — repo-local fallback
      5. /sessions/*/mnt/Documents/Claude/.env — Cowork sandbox path

    Keys already in os.environ (e.g. set by a cron or shell export) are never
    overwritten.
    """
    candidates = [
        os.path.expanduser("~/Documents/Claude/.env"),
        os.path.expanduser("~/.claude/.env"),
        os.path.expanduser("~/.env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    # Cowork sandbox: the user's Documents folder is mounted under /sessions/*/mnt/
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
FC_KEY = os.environ.get("FIRECRAWL_API_KEY", "")


def _warn_missing_keys():
    """Print one-line stderr warning per missing key at startup.

    Without this, missing keys cause the waterfall to silently no-op and
    return "not_found" — indistinguishable from a real coverage miss.
    """
    if not APOLLO_KEY:
        print(
            "WARNING: APOLLO_API_KEY not loaded — primary lookup disabled. "
            "Set it in ~/Documents/Claude/.env (https://app.apollo.io/#/settings/integrations/api).",
            file=sys.stderr,
        )
    if not FC_KEY:
        print(
            "WARNING: FIRECRAWL_API_KEY not loaded — fallback website scrape disabled. "
            "Set it in ~/Documents/Claude/.env (https://www.firecrawl.dev/app/api-keys).",
            file=sys.stderr,
        )


_warn_missing_keys()


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(url, payload, headers):
    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json", **headers})
        with urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": str(e)}


# ── Apollo ────────────────────────────────────────────────────────────────────

def apollo_finder(first, last, domain):
    """Apollo People Match — uses curl to avoid urllib 403 from Apollo's WAF."""
    if not APOLLO_KEY:
        return None, None
    payload = json.dumps({
        "first_name": first, "last_name": last,
        "domain": domain, "reveal_personal_emails": False,
    })
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.apollo.io/v1/people/match",
             "-H", f"x-api-key: {APOLLO_KEY}",
             "-H", "Content-Type: application/json",
             "-H", "Cache-Control: no-cache",
             "-d", payload],
            capture_output=True, text=True, timeout=25
        )
        r = json.loads(result.stdout)
        person = r.get("person") or {}
        email = person.get("email")
        title = person.get("title")  # caller can surface this
        if email and "@" in email:
            return email, "apollo"
        # Even without email, Apollo confirms the person exists at this org.
        # Surface that as a hint by returning the title in a side channel.
        if person.get("name"):
            return None, f"apollo_no_email:{title or 'unknown_title'}"
    except Exception as e:
        print(f"WARNING: Apollo call failed for {first} {last} @ {domain}: {e}", file=sys.stderr)
    return None, None


# ── Firecrawl ─────────────────────────────────────────────────────────────────

def _fc_scrape(url):
    """Scrape a single URL with Firecrawl, return plain text."""
    if not FC_KEY:
        return ""
    r = _post(
        "https://api.firecrawl.dev/v1/scrape",
        {"url": url, "formats": ["markdown"], "onlyMainContent": True},
        {"Authorization": f"Bearer {FC_KEY}"},
    )
    if r.get("success"):
        return r.get("data", {}).get("markdown", "") or ""
    return ""

def _extract_emails_from_text(text, domain):
    """Pull all email addresses from text that match the target domain."""
    found = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
    root = domain.lower().split(".")[0]
    return [e.lower() for e in found if root in e.lower()]

def _name_score(email, first, last):
    """Score how well an email matches a person's name. Higher = better match."""
    local = email.split("@")[0].lower()
    fl = (first or "").lower()
    ll = (last or "").lower()
    score = 0
    if fl in local: score += 2
    if ll in local: score += 2
    if fl[0] in local: score += 1
    return score

def firecrawl_finder(first, last, domain, website_url=None):
    """Scrape firm website pages looking for an email matching this person.

    Skips pages that only carry generic mailboxes (info@, support@, hello@) —
    those don't help personalized outreach.
    """
    if not FC_KEY:
        return None, None

    pages = []
    if website_url:
        pages.append(website_url)
    pages += [
        f"https://{domain}/team",
        f"https://{domain}/about",
        f"https://{domain}/people",
        f"https://{domain}/contact",
        f"https://{domain}/our-team",
        f"https://{domain}/staff",
    ]

    GENERIC_LOCALS = {"info", "hello", "contact", "support", "billing", "sales", "admin", "office", "team"}
    best_email = None
    best_score = 0

    for page_url in pages:
        text = _fc_scrape(page_url)
        if not text:
            continue
        for email in _extract_emails_from_text(text, domain):
            local = email.split("@")[0].lower()
            if local in GENERIC_LOCALS:
                continue
            score = _name_score(email, first, last)
            if score > best_score:
                best_score = score
                best_email = email
        if best_score >= 4:
            break

    if best_email and best_score >= 2:
        return best_email, "firecrawl"
    return None, None


# ── Pattern fallback ──────────────────────────────────────────────────────────

def guess_pattern(first, last, domain):
    """Return the single most-likely email pattern. Unverified — caller should
    treat this as a low-confidence guess (status="pattern_guess")."""
    fl = (first or "").lower().strip()
    ll = (last or "").lower().strip()
    if not fl or not ll or not domain:
        return None
    # Most common B2B pattern (and the one Apollo confirmed for Bob Myman):
    # firstinitial+lastname@domain → e.g. rmyman@mymangreenspan.com
    return f"{fl[0]}{ll}@{domain}"

def is_sane(email, domain):
    if not email or "@" not in email:
        return False
    _, dom = email.rsplit("@", 1)
    dom = dom.lower()
    if any(dom.endswith(t) for t in [".fj", ".ga", ".tk", ".ml", ".cf"]):
        return False
    exp_root = domain.lower().split(".")[0]
    if exp_root not in dom and dom.split(".")[0] not in exp_root:
        if len(exp_root) < 5 or exp_root[:5] not in dom:
            return False
    return True


# ── Main waterfall ────────────────────────────────────────────────────────────

def find_email_for_person(first, last, domain, website_url=None):
    if not first or not last or not domain:
        return {"first": first, "last": last, "domain": domain, "email": None, "method": "missing_input"}

    # 1. Apollo (verified)
    email, method = apollo_finder(first, last, domain)
    if email and is_sane(email, domain):
        return {"first": first, "last": last, "domain": domain, "email": email, "method": method}

    # Apollo found the person but no email — preserve the title hint
    apollo_title_hint = method if method and method.startswith("apollo_no_email:") else None

    # 2. Firecrawl website scrape
    email, method = firecrawl_finder(first, last, domain, website_url)
    if email and is_sane(email, domain):
        result = {"first": first, "last": last, "domain": domain, "email": email, "method": method}
        if apollo_title_hint:
            result["apollo_title"] = apollo_title_hint.split(":", 1)[1]
        return result

    # 3. Pattern guess (UNVERIFIED — caller decides whether to use)
    pattern = guess_pattern(first, last, domain)
    if pattern and is_sane(pattern, domain):
        result = {"first": first, "last": last, "domain": domain, "email": pattern, "method": "pattern_guess"}
        if apollo_title_hint:
            result["apollo_title"] = apollo_title_hint.split(":", 1)[1]
        return result

    return {"first": first, "last": last, "domain": domain, "email": None, "method": "not_found"}


def batch(contacts):
    return [
        find_email_for_person(
            c.get("first"), c.get("last"), c.get("domain"),
            website_url=c.get("website_url"),
        )
        for c in contacts
    ]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--names-json", required=True, help='JSON list of {first, last, domain, website_url?}')
    args = p.parse_args()
    contacts = json.loads(args.names_json)
    result = batch(contacts)
    print(json.dumps(result, indent=2))
