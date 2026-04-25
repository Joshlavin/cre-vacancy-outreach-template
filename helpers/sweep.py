#!/usr/bin/env python3
"""Address-reverse-lookup tenant discovery via Firecrawl Search.

Inverts the discovery problem: instead of asking "who's at this building?"
we enumerate plausible suite addresses on adjacent floors and reverse-lookup
each one. The string `"11601 Wilshire Blvd" "Suite 2200"` is specific enough
that Google returns only the actual occupant.

Why this works for high-rises that LA City BL data misses:
  - Companies don't need to be in any registry — just have ANY web presence
    that lists their full address (their own site, Google Business, MapQuest,
    Yelp, state bar, FINRA, etc.)
  - Coverage is roughly Google's full index of the building, regardless of
    building age, city, or tenant size
  - Per-suite queries naturally target only the floors we care about

Cost: ~30-60 Firecrawl search calls per vacancy, ~1 credit each on the
free tier (500 credits/month).

Usage:
  python3 sweep.py "11601 Wilshire Blvd, Los Angeles, CA 90025" --suite 2360
  python3 sweep.py "11601 Wilshire Blvd, Los Angeles, CA 90025" --suite 2360 --step 10
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def _load_env():
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
FC_KEY = os.environ.get("FIRECRAWL_API_KEY", "")


# ── Skip patterns (mirror discover.py + extras for sweep noise) ───────────────
SKIP_PATTERNS = [
    # Coworking
    r"\bregus\b", r"\bwework\b", r"\bindustrious\b", r"\bconvene\b", r"\bknotel\b",
    r"\bcompass offices?\b", r"\bspaces by regus\b", r"\bservcorp\b",
    r"\bpremier business centers?\b", r"\bpremier workspaces?\b",
    # Brokerages
    r"\bcbre\b", r"\bcushman\b", r"\bjll\b", r"\bcolliers\b", r"\bkidder mathews\b",
    r"\bavison young\b", r"\bcharles dunn\b", r"\bmadison partners\b",
    r"\bla realty partners\b", r"\bnewmark\b", r"\btranswestern\b", r"\bsavills\b",
    r"\bkitty wallace\b",          # Colliers team @ 11601 Wilshire 2350
    r"\bwallace team\b",           # Same — surfaces under team name on Floor 4
    r"\blos angeles housing\b",    # City of LA, not a tenant
    r"\bforeign tourist\b",        # Directory page
    r"\binternational tourist\b",  # Directory page
    r"\bnorth america firms\b",    # Directory page
    r"\bnpi #\d+\b",               # Medical NPI lookup pages
    r"\bcanadian trademarks\b",    # Canadian IP lookup
    r"\bsponsors?\b",               # Conference/event sponsor pages
    r"\b(vineyard|wedding) (weddings|venues|guests)\b",  # Wedding venue spam
    r"\b(town guide|local guide)\b",
    r"\bvivinavi\b",                # Japanese-LA listings directory
    r"\bcelebrities? (autograph|fan mail)\b",
    r"\bhow to contact celebrities\b",
    r"\bnational immigration legal services\b",  # Directory
    r"\bdivision of hiv\b",         # County health pages
    r"\bagencies\s*&\s*managers\b", # Talent agency directory
    r"\b(business credit|commercial credit) report\b",
    r"\bfamily limited partnership\b",       # Personal wealth shells
    r"\bplan holders?\b",                    # Procurement bid notice pages
    r"\bcompany snapshot\b",                 # FMCSA carrier lookup
    r"\bforeign government tourist\b",       # Tourism ministry directory
    r"\btalent agencies? for\b",             # Talent directory ("LA Talent Agencies for Voice")
    r"^all\s+\w+(\s+\w+)?\s+office locations?\b",  # "All Microsoft Office Locations"
    r"^contact\s+\w+\s+(today|now|us)\b",    # CTA pages, not company name
    r"^learn more about\b",                  # CTA fragment
    # Landlords / developers
    r"\bhudson pacific\b", r"\bbrookfield\b", r"\bonni\b", r"\bhines\b",
    r"\brelated companies\b", r"\bboston properties\b", r"\bdouglas emmett\b",
    # Aggregators (when they slip into the title field)
    r"\bloopnet\b", r"\bcrexi\b", r"\bcompstak\b", r"\bcommercialcafe\b",
    # Generic / noise
    r"\bzip code\b", r"\bsearch results\b",
]

GENERIC_TITLES = {"home", "contact", "contact us", "about", "about us",
                  "team", "the team", "directory", "tenants", "leasing",
                  "for lease", "for rent", "office space", "available",
                  "untitled", "no title"}

# Title patterns that mean "this is a document, not a company name"
NOISE_TITLE_PATTERNS = [
    r"^\[pdf\]", r"^\[xls\]", r"^\[doc\]",        # File-type prefixes
    r"^form\s+[a-z]",                             # SEC forms (Form D, Form N)
    r"^(january|february|march|april|may|june|"   # Month-only titles
    r"july|august|september|october|november|december)\s+\d{4}$",
    r"bankruptcy court",
    r"\b(detail by entity|listings? in|search results?)\b",
    r"^law firm id",
    r"^wilshire blvd",                            # PDF that's just address
    r"^\d+\s+wilshire",                           # Bare address-as-title (any street)
    r"^[\d\s,/.-]+$",                             # Pure numbers/punctuation
    r"\btrademark\b",                             # USPTO trademark filings
    r"\b\w+\.(mbx|pdf|doc|docx|xls|xlsx|csv)$",  # File extensions in title
    r"^(attorneys?|lawyers?|doctors?|brokers?|agents?)$",  # Generic profession nouns
    # Directory/aggregator page titles — "Los Angeles, CA Bankruptcy Law Firms"
    r"^(los angeles|la|west la|brentwood|santa monica|beverly hills|century city|"
    r"westwood|san francisco|sf|new york|chicago).*\b(law(yers?)?|firm|attorneys?|"
    r"lawyers?|doctors?)\b",
    r"^best\s+\w+",                               # "Best X Lawyers" review pages
    r"^https?://",                                # URLs as titles
    r"^contact\b\s*\d*$",                         # "Contact 2019" / "Contact"
    r"^table\b",                                  # "[XLS] Table"
    r"^untitled\b",
    r"^table of contents",
    r"\bapi/views\b",                             # Government data URLs
    r"^(disclaimer|privacy policy|terms|cookies?)\b",  # Legal page titles
    r"\b(parties? for|v\.\s+\w+|\bmotion\b|\bcomplaint\b)",  # Court case titles
    r"\b\w+_\w+\s+technical proposal\b",          # Govt RFP responses
    r"^(may|june|january|february|march|april|"   # Month-only (already had — strengthen)
    r"july|august|september|october|november|december)$",
    r"^helping\b",                                # "Helping low-income..." nonprofit pages
    r"\boxalis\b",                                # Some LA legal directory
    r"\bsimmler\b",                               # Same — directory under random brand
    r"^200\+ guests\b",                           # Wedding/event venue templates
    # City + practice combos that are clearly directory pages
    r"^[a-z\s]+(beach|valley|hills|park|heights),\s*ca\s+\w+",
    r"\battorneys?\s+near me\b",
    r"\blawyers?\s+near me\b",
    r"\bnear\s+me$",
    # Court case citation patterns
    r"^[\w\s]+\s+v\.?\s+[\w\s]+\s+\(\d",
    # Government program titles
    r"\b(division of|department of|bureau of|office of)\s+\w+",
    # News article lead-ins
    r"^(ahead of|amid|despite|after|before|during|following|in light of)\b",
    # Government/policy news patterns
    r"\b(homeless|housing|fire|earthquake|wildfire|disaster|pandemic) (services|department|response|relief)\b",
    # Generic non-company titles that survive extraction
    r"^(details?|map|maps|contact information|contact us|directions|menu|"
    r"location|locations|hours|reviews?|photos?|gallery|blog|news|home|index|"
    r"page \d+|results|listings?)$",
    # Real estate aggregator listing titles
    r"\boffice space (for|near|in)\s+(rent|lease|me|los angeles|la)\b",
    r"\bcommercial (real estate|space|property) (for|in)\b",
    # News/blog patterns
    r"^(three|four|five|two|one)\s+(deaths?|injuries?|arrests?|shot)\b",
    r"\bscholarships? for\b",                     # Scholarship directory pages
    r"^growing generations\b",                    # Specific noise (LA surrogacy agency unrelated)
    r"\bsag franchised agents\b",                 # SAG talent directory
    # "Top X, CA Y Lawyers Near You" — Avvo/Justia directory template
    r"^top\s+[\w\s]+,?\s+ca\s+[\w\s]+(lawyer|law|attorney|firm)s?",
    # "Best/Top X Lawyers" without state — same template
    r"^(top|best)\s+[\w\s]+(lawyer|attorney|firm|services)s?\s+near\b",
    # Form 990 / nonprofit filing
    r"\breturn of organization exempt\b",
    r"\bform\s+990\b",
    # State-just-name-of-city titles
    r"^[a-z]+\s+park,?\s+ca\s*$",
    r"^huntington park,?\s+ca\s*$",
    # Government regional bodies
    r"\b(regional|state|local) (water|air|transportation|housing) (resources|quality|board)\b",
    r"\bnpdes?\b",                                # NPDES water permits
]


# Titles that need at least N capitalized "company-ish" tokens
# (filters out "abundance capital 2026" → real, vs "Three deaths" → not)
def looks_like_company_name(title):
    """Cheap heuristic: title has at least one capitalized word that isn't
    a calendar word, an article, or a sentence-start verb.

    Accepts both mixed-case ("Capital", "EpamSystems") and all-caps acronyms
    of 2-5 letters ("IEQ", "EPAM", "LLP", "LLC") that often appear in firm names.
    """
    if not title:
        return False
    # Mixed-case capitalized tokens
    caps = re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)*\b", title)
    # All-caps acronyms 2-5 letters (covers IEQ, EPAM, LLP, LLC, NPDES, etc.)
    acronyms = re.findall(r"\b[A-Z]{2,5}\b", title)
    caps = caps + acronyms
    if not caps:
        return False
    # Filter sentence-start verbs and dates
    skip = {"The", "A", "An", "This", "That", "These", "Those", "And", "Or", "But",
            "Three", "Four", "Five", "Two", "Six", "Seven", "Eight", "Nine", "Ten",
            "Ahead", "Amid", "Despite", "After", "Before", "During", "Following",
            "Map", "Details", "Contact", "Learn", "Read", "View", "See", "Find",
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
            "Office", "Space", "Rent", "Lease", "Page",
            "Los", "Angeles", "California", "Beverly", "Hills", "Brentwood",
            "Santa", "Monica", "Westwood", "Westside", "West",
            "Wilshire", "Boulevard", "Blvd", "Suite", "Ste",
            # Common all-caps acronyms that aren't company names
            "USA", "USCA", "ABA", "FAQ", "PDF", "XLS", "DOC", "URL", "NPI",
            "SEC", "IRS", "DOT", "CA", "NY", "LP", "LLC", "LLP", "INC", "CORP",
            "NPDES", "IPP", "PHE", "EBT", "ETC"}
    real = [c for c in caps if c not in skip]
    return len(real) >= 1

# URL hosts that mean "this is a directory/registry/document, not a company site"
NOISE_URL_HOSTS = [
    "uscourts.gov", "pacer.gov", "sec.gov", "uspto.gov", "tsdrapi.uspto.gov",
    "trademarkia.com", "trademarks.justia.com",
    "ftb.ca.gov", "edd.ca.gov", "lacity.org", "data.lacity.org",
    "search.sunbiz.org",                          # Florida SOS — "Detail by Entity"
    "veritaglobal.net",                           # bankruptcy archives
    # Aggregators are fine for confirmation, but bad as the *only* source
    # — handled separately by requiring a non-aggregator hit when possible
]


def is_skipped(name):
    n = (name or "").lower().strip()
    if n in GENERIC_TITLES or len(n) < 3:
        return True
    if any(re.search(p, n) for p in NOISE_TITLE_PATTERNS):
        return True
    return any(re.search(p, n) for p in SKIP_PATTERNS)


def is_noise_url(url):
    u = (url or "").lower()
    return any(host in u for host in NOISE_URL_HOSTS)


def normalize_company(name):
    """Normalize for dedup. Strip 'Los Angeles', 'LA Office', trailing geo, suffixes."""
    n = (name or "").lower().strip()
    # Drop trailing geo qualifiers
    n = re.sub(r"\s+(los angeles|la|new york|chicago|san francisco|sf)(,?\s+(ca|ny|il)\s*\d*)?\s*(office|hq|headquarters)?\s*$", "", n)
    n = re.sub(r"\s+(office|hq|headquarters)\s*$", "", n)
    # Drop common suffixes for matching
    n = re.sub(r"[,\s]+(inc|incorporated|llc|llp|lp|pc|pllc|corp|corporation|company|co|ltd|limited)\.?\s*$", "", n)
    return n.strip()


# ── Firecrawl wrapper ─────────────────────────────────────────────────────────

def _fc_search_uncached(query, limit=3):
    """Call Firecrawl /v1/search (uncached). Returns list of {title, url, description}."""
    if not FC_KEY:
        return []
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.firecrawl.dev/v1/search",
             "-H", f"Authorization: Bearer {FC_KEY}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"query": query, "limit": limit})],
            capture_output=True, text=True, timeout=30,
        )
        r = json.loads(result.stdout) if result.stdout else {}
        return r.get("data", []) or []
    except Exception as e:
        print(f"WARNING: fc_search err for '{query[:60]}': {e}", file=sys.stderr)
        return []


def fc_search(query, limit=3):
    """Cached wrapper around Firecrawl search. Same query within 30 days
    returns the same results — solves Firecrawl's ranking variance and
    saves credits on re-runs."""
    try:
        from helpers import search_cache
    except ImportError:
        # Standalone import path
        import importlib.util, os
        spec = importlib.util.spec_from_file_location(
            "search_cache",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_cache.py"),
        )
        search_cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(search_cache)
    # Bake the limit into the cache key so different limits cache separately
    cache_query = f"L{limit}::{query}"
    return search_cache.cached_search(
        cache_query,
        lambda q: _fc_search_uncached(query, limit),
        ttl_days=30,
    )


# ── Suite enumeration ─────────────────────────────────────────────────────────

def parse_suite_num(suite):
    m = re.search(r"\d+", str(suite))
    return int(m.group(0)) if m else None


def detect_format(suite_num):
    """3-digit (low-rise: floor 1-9) or 4-digit (high-rise: floor 10+)."""
    if suite_num is None:
        return 3
    return 4 if suite_num >= 1000 else 3


def suite_to_floor(n, fmt):
    if n is None:
        return None
    return n // 100  # works for both 3- and 4-digit conventions


def enumerate_suites(vacancy_suite, step=10, also_step5_pass=False):
    """Generate candidate suites on adjacent floors. Skips vacancy itself."""
    n = parse_suite_num(vacancy_suite)
    if n is None:
        return []
    fmt = detect_format(n)
    vacancy_floor = suite_to_floor(n, fmt)
    out = []
    floors = [f for f in (vacancy_floor - 1, vacancy_floor, vacancy_floor + 1) if f >= 1]
    for floor in floors:
        floor_base = floor * 100
        for offset in range(0, 100, step):
            candidate = floor_base + offset
            if candidate == n:
                continue
            out.append(candidate)
    # Optional second pass with finer step (catches 2305, 2315, etc.)
    if also_step5_pass and step != 5:
        seen = set(out)
        for floor in floors:
            floor_base = floor * 100
            for offset in range(5, 100, 10):  # 5, 15, 25...
                candidate = floor_base + offset
                if candidate == n or candidate in seen:
                    continue
                out.append(candidate)
    return out


# ── Address variants ──────────────────────────────────────────────────────────

_TYPE_EXPAND = {
    " Blvd": " Boulevard", " Boulevard": " Blvd",
    " Ave": " Avenue", " Avenue": " Ave",
    " Pkwy": " Parkway", " Parkway": " Pkwy",
    " Rd": " Road", " Road": " Rd",
    " Dr": " Drive", " Drive": " Dr",
}


def address_variants(address):
    """Return distinct address strings to try in queries.
    For "11601 Wilshire Blvd, Los Angeles, CA 90025" returns
    ["11601 Wilshire Blvd", "11601 Wilshire Boulevard"]."""
    main = address.split(",")[0].strip()
    out = {main}
    for short, long_ in _TYPE_EXPAND.items():
        if short in main:
            out.add(main.replace(short, long_))
    return list(out)


# ── Result parsing ────────────────────────────────────────────────────────────

# Drop everything after the first separator that looks like a title divider
_TITLE_SPLIT = re.compile(r"\s*[|\-–—]\s*|\s*·\s*")


def extract_company(title, description, suite_num, url=""):
    """Extract the most likely company name from a search result.

    Strategy:
      1. Verify suite_num appears in title or description — drop if not
      2. Try title before first separator
      3. If title is "Person, Title at Firm" pattern → use Firm
      4. If extracted name looks generic ("Los Angeles Law Office") → fall back
         to URL hostname → company name guess
    """
    if not title:
        return None
    blob = f"{title} {description or ''}".lower()
    suite_str = str(suite_num)
    if not any(p in blob for p in [
        f"suite {suite_str}", f"ste {suite_str}", f"ste. {suite_str}",
        f"#{suite_str}", f"unit {suite_str}",
    ]):
        return None

    # Pattern: "Name, Role at Company" or "Name — Role | Company"
    at_match = re.search(r"\bat\s+([A-Z][\w\s&,'.-]+?)(?:\s*[\-|·]|\s*$)", title)
    if at_match:
        firm = at_match.group(1).strip(" ,.:")
        if 3 <= len(firm) <= 120 and not is_skipped(firm):
            return firm[:120]

    # Default: title before first separator
    chunks = _TITLE_SPLIT.split(title, maxsplit=1)
    company = chunks[0].strip()
    company = re.sub(
        r"\s*\b(yelp|mapquest|maps|loopnet|crexi|google|wikipedia|facebook|linkedin|"
        r"bbb|better business bureau|find a|search for|in los angeles|los angeles ca|"
        r"company profile|profile)\b.*$",
        "", company, flags=re.I,
    ).strip(" ,.:")

    # If too generic, try to derive from URL hostname
    if (len(company) < 3 or
            re.match(r"^(los angeles|la)\s+(law|legal|medical|office)", company.lower())):
        host = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
        if host:
            dom = host.group(1).split(".")[0]
            # Camelcase split: "makaremlaw" stays as is; user readable in draft
            if dom and dom not in {"www", "mail", "go", "find", "search"}:
                return dom[:60]
        return None

    return company[:120]


def url_domain(url):
    """Get the registrable-ish domain from a URL for cross-result dedup."""
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    if not m:
        return ""
    host = m.group(1).lower()
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


# Hosts that are directories/aggregators — NOT the firm's own site
DIRECTORY_HOSTS = {
    "mapquest.com", "yelp.com", "yellowpages.com", "bbb.org", "facebook.com",
    "linkedin.com", "google.com", "wikipedia.org", "twitter.com", "x.com",
    "findlaw.com", "lawinfo.com", "lawyers.com", "justia.com", "avvo.com",
    "martindale.com", "superlawyers.com",
    "loopnet.com", "crexi.com", "compstak.com", "commercialcafe.com",
    "myvisajobs.com", "zocdoc.com", "healthgrades.com", "vitals.com",
    "pitchbook.com", "crunchbase.com", "zoominfo.com", "rocketreach.co",
    "opencorporates.com", "bizapedia.com", "sec.gov", "uspto.gov",
    "clearlyrated.com", "trustpilot.com",
}


def is_directory_host(domain):
    return domain in DIRECTORY_HOSTS or domain.endswith(".gov")


def looks_like_firms_own_site(url, company_name):
    """Heuristic: is this URL on the firm's own website?

    True when the domain is NOT a known directory AND the company name
    contains tokens from the domain (or vice versa).
    """
    domain = url_domain(url)
    if not domain or is_directory_host(domain):
        return False
    if not company_name:
        return False
    # Tokenize both
    domain_root = domain.split(".")[0].lower()
    name_tokens = re.findall(r"[a-z]+", company_name.lower())
    if not name_tokens:
        return False
    # Match if domain root contains any name token (>=4 chars) or vice versa
    for token in name_tokens:
        if len(token) >= 4 and (token in domain_root or domain_root in token):
            return True
    # Also accept if first 4 chars of joined-tokens match domain root
    joined = "".join(name_tokens)
    if len(joined) >= 4 and len(domain_root) >= 4:
        if joined[:4] == domain_root[:4]:
            return True
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def _prettify_domain_name(name):
    """Convert 'makaremlaw' → 'Makarem Law', 'foobarcapital' → 'Foobar Capital'."""
    if not name or not name.islower():
        return name
    # Common compound suffixes
    for suf in ["law", "capital", "partners", "advisors", "group", "holdings",
                "ventures", "associates", "services", "media", "consulting"]:
        if name.endswith(suf) and len(name) > len(suf) + 2:
            base = name[:-len(suf)]
            return f"{base.title()} {suf.title()}"
    return name.title()


def sweep(address, vacancy_suite, step=10, per_query_limit=3, max_queries=60,
          concurrency=6, second_pass_threshold=3):
    """Run the address-reverse-lookup discovery (parallelized, cached).

    If the initial pass returns < second_pass_threshold tenants, automatically
    runs a finer step=5 second pass to catch odd-numbered suites in dense
    buildings.

    Returns:
      {
        "address": ..., "vacancy_suite": ..., "vacancy_floor": ...,
        "building_format": 3 | 4, "candidates_enumerated": int,
        "queries_made": int, "second_pass_ran": bool,
        "tenants": [{"company", "suite", "floor", "relation",
                     "evidence_url", "evidence_title", "confidence", "source"}],
      }
    """
    if not FC_KEY:
        return {"_error": "FIRECRAWL_API_KEY not set", "tenants": []}

    n = parse_suite_num(vacancy_suite)
    fmt = detect_format(n)
    vacancy_floor = suite_to_floor(n, fmt)
    candidates = enumerate_suites(vacancy_suite, step=step)[:max_queries]
    variants = address_variants(address)

    # Build the full query list: (suite, query_str)
    queries = [(suite, f'"{variants[0]}" "Suite {suite}"') for suite in candidates]
    second_pass_ran = False

    # Parallel fetch
    raw_results = []  # list of (suite, [results])
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        future_to_suite = {ex.submit(fc_search, q, per_query_limit): s for s, q in queries}
        for fut in as_completed(future_to_suite):
            suite = future_to_suite[fut]
            try:
                raw_results.append((suite, fut.result()))
            except Exception as e:
                print(f"WARNING: sweep query failed for suite {suite}: {e}", file=sys.stderr)

    # Merge / dedup
    found = {}  # norm_name → tenant dict
    for suite, results in raw_results:
        for r in results:
            if is_noise_url(r.get("url", "")):
                continue
            title = r.get("title", "") or ""
            desc = r.get("description", "") or ""
            url = r.get("url", "") or ""
            company = extract_company(title, desc, suite, url=url)
            if not company:
                continue
            if is_skipped(company):
                continue
            # Reject titles that don't have any company-shaped capitalized tokens
            if not looks_like_company_name(company):
                continue
            norm = normalize_company(company)
            domain = url_domain(url)

            firms_own_now = looks_like_firms_own_site(url, company)
            domain_hint_now = domain if firms_own_now else None

            # Dedup primary key, in priority order:
            #   1. Same suite AND same domain_hint (firm's own site, both pass)
            #   2. Same suite AND same evidence-URL domain
            #   3. Same suite AND same normalized name
            existing_match = None
            for k, v in found.items():
                if v["suite"] != str(suite):
                    continue
                # Match by firm's own domain
                if domain_hint_now and v.get("domain_hint") == domain_hint_now:
                    existing_match = k
                    break
                # Match by evidence URL domain
                if domain and v.get("_domain") == domain:
                    existing_match = k
                    break
                # Match by normalized name
                if k == norm:
                    existing_match = k
                    break

            tenant_floor = suite_to_floor(suite, fmt)
            if tenant_floor == vacancy_floor:
                relation = "same floor"
            elif tenant_floor == vacancy_floor + 1:
                relation = "floor above"
            elif tenant_floor == vacancy_floor - 1:
                relation = "floor below"
            else:
                relation = f"floor {tenant_floor}"

            firms_own = firms_own_now
            base_conf = 0.95 if firms_own else 0.85
            domain_hint = domain_hint_now

            if existing_match is None:
                found[norm] = {
                    "company": _prettify_domain_name(company),
                    "suite": str(suite),
                    "floor": tenant_floor,
                    "relation": relation,
                    "evidence_url": url,
                    "evidence_title": title[:140],
                    "confidence": base_conf,
                    "source": "address_sweep",
                    "_domain": domain,
                    "domain_hint": domain_hint,
                }
            else:
                # Multi-source confirmation
                found[existing_match]["confidence"] = min(
                    0.97, found[existing_match]["confidence"] + 0.05
                )
                # Promote if this hit is the firm's own site and prior wasn't
                if firms_own and not found[existing_match].get("domain_hint"):
                    found[existing_match]["company"] = _prettify_domain_name(company)
                    found[existing_match]["evidence_url"] = url
                    found[existing_match]["evidence_title"] = title[:140]
                    found[existing_match]["domain_hint"] = domain_hint
                    found[existing_match]["confidence"] = max(
                        found[existing_match]["confidence"], 0.95
                    )

    # Second pass: if recall is sparse, run a finer step=5 pass for the
    # offsets we didn't try (5, 15, 25, ...). Cached results are free.
    if step >= 10 and len(found) < second_pass_threshold:
        second_pass_ran = True
        n_val = n
        fmt_val = fmt
        floors = [f for f in (vacancy_floor - 1, vacancy_floor, vacancy_floor + 1) if f >= 1]
        extra_candidates = []
        for floor in floors:
            base = floor * 100
            for offset in range(5, 100, 10):  # 5, 15, 25, ...
                cand = base + offset
                if cand == n_val:
                    continue
                extra_candidates.append(cand)
        extra_queries = [(s, f'"{variants[0]}" "Suite {s}"') for s in extra_candidates[:max_queries]]
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            future_to_suite = {ex.submit(fc_search, q, per_query_limit): s for s, q in extra_queries}
            for fut in as_completed(future_to_suite):
                suite = future_to_suite[fut]
                try:
                    raw_results.append((suite, fut.result()))
                except Exception as e:
                    print(f"WARNING: 2nd-pass failed for suite {suite}: {e}", file=sys.stderr)
        # Re-merge — same logic as the main loop. Simpler: re-run merge over all raw_results.
        # (For brevity, we just rerun the per-result loop on the new ones.)
        new_raw = raw_results[-len(extra_queries):]
        for suite, results in new_raw:
            for r in results:
                if is_noise_url(r.get("url", "")):
                    continue
                title = r.get("title", "") or ""
                desc = r.get("description", "") or ""
                url = r.get("url", "") or ""
                company = extract_company(title, desc, suite, url=url)
                if not company or is_skipped(company) or not looks_like_company_name(company):
                    continue
                norm = normalize_company(company)
                domain = url_domain(url)
                firms_own_now = looks_like_firms_own_site(url, company)
                domain_hint_now = domain if firms_own_now else None
                existing_match = None
                for k, v in found.items():
                    if v["suite"] != str(suite):
                        continue
                    if domain_hint_now and v.get("domain_hint") == domain_hint_now:
                        existing_match = k; break
                    if domain and v.get("_domain") == domain:
                        existing_match = k; break
                    if k == norm:
                        existing_match = k; break
                tenant_floor = suite_to_floor(suite, fmt)
                if tenant_floor == vacancy_floor:
                    relation = "same floor"
                elif tenant_floor == vacancy_floor + 1:
                    relation = "floor above"
                elif tenant_floor == vacancy_floor - 1:
                    relation = "floor below"
                else:
                    relation = f"floor {tenant_floor}"
                base_conf = 0.95 if firms_own_now else 0.85
                if existing_match is None:
                    found[norm] = {
                        "company": _prettify_domain_name(company),
                        "suite": str(suite), "floor": tenant_floor, "relation": relation,
                        "evidence_url": url, "evidence_title": title[:140],
                        "confidence": base_conf, "source": "address_sweep",
                        "_domain": domain, "domain_hint": domain_hint_now,
                    }
                else:
                    found[existing_match]["confidence"] = min(
                        0.97, found[existing_match]["confidence"] + 0.05
                    )

    tenants = sorted(found.values(),
                     key=lambda t: (-t["confidence"], t["floor"]))
    for t in tenants:
        t.pop("_domain", None)
    queries_made = len(raw_results)

    return {
        "address": address,
        "vacancy_suite": vacancy_suite,
        "vacancy_floor": vacancy_floor,
        "building_format": fmt,
        "candidates_enumerated": len(candidates),
        "queries_made": queries_made,
        "second_pass_ran": second_pass_ran,
        "tenants": tenants,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("address")
    p.add_argument("--suite", required=True)
    p.add_argument("--step", type=int, default=10,
                   help="Enumerate suites every N (default 10). Use 5 for fine pass.")
    p.add_argument("--limit", type=int, default=3, help="Search results per query.")
    p.add_argument("--max-queries", type=int, default=60,
                   help="Cap on total Firecrawl searches per run.")
    args = p.parse_args()

    if not FC_KEY:
        print("ERROR: FIRECRAWL_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    result = sweep(args.address, args.suite, step=args.step,
                   per_query_limit=args.limit, max_queries=args.max_queries)
    print(json.dumps(result, indent=2, default=str))
