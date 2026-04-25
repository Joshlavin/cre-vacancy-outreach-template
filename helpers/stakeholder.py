#!/usr/bin/env python3
"""LLM-driven decision-maker selection.

Does NOT call Anthropic API. The Claude session that runs the SKILL is the
orchestrator; this module just formats the prompt and parses the response.
That keeps every judgment visible in the chat history (auditable) and
avoids burning API credits.

Workflow:
  1. helpers/apollo.py roster_for_domain(...) returns 10 people
  2. stakeholder.batch_prompt([{company, domain, org, roster}, ...]) builds
     a single mega-prompt covering all companies at this building
  3. The SKILL (Claude) reads the prompt, judges, replies with JSON
  4. stakeholder.parse_batch_response(response_text, batches) returns
     {company: [picked_people]}
  5. emails.py finds verified emails for each pick

Verbatim port of /Users/joshlavin/Documents/Claude/.tmp/kurt-test/kurt_agent/stakeholder.py
with one addition: company size now also drives the picker rules instead of
title-only matching.
"""
import json
import re
from typing import List, Dict, Any, Tuple


SYSTEM = """You are identifying the right decision maker for a commercial real estate leasing outreach email about a SPECIFIC suite vacancy in a SPECIFIC LA-area building.

Given a company's context and a roster of people at that company, pick the 1-2 people MOST LIKELY to make or influence the lease/office-space decision FOR THIS LA OFFICE.

CRITICAL — LA OFFICE RULE (overrides everything else):
  The vacancy is at an LA-area address. We are pitching the LA-office head.
  - For multi-office firms (Apollo description mentions multiple cities, OR roster shows people in non-LA cities), STRONGLY prefer roster members whose city field is in the LA metro: Los Angeles, Beverly Hills, Santa Monica, Brentwood, West Hollywood, Westwood, Culver City, Pasadena, Burbank, Glendale, El Segundo, Marina Del Rey, Playa Vista.
  - If NO roster member is LA-based AND the company has 50+ employees, return picks:[] with _skip_reason "no_la_office_head_named". Do NOT pitch a SF or NYC executive about an LA satellite office vacancy.
  - For solo/micro firms (1-10 employees), if the founder is in any city it's fine — they ARE the company.
  - For 10-50 employee firms, prefer LA but accept HQ if HQ is the only contact.

Rules by company profile (after applying LA office rule):
- Solo / micro (1-10 employees): Founder, Owner, Sole Partner. Skip everyone else.
- Small (10-50): Managing Partner, COO, Head of Operations, CFO. Skip rank-and-file producers/attorneys/doctors.
- Mid (50-200): Head of LA Office, Office Manager, Director of Real Estate/Facilities, COO, CFO, Director of Operations.
- Enterprise (200+): LA Office Managing Partner, LA Branch Head, Head of Real Estate, Head of Facilities. If no LA-named role, return picks:[] with _skip_reason "no_la_office_head_named".
- Medical practice: Practice Manager OR Owner-Doctor (whoever's on the lease).
- Law firm: LA Office Managing Partner OR Name Partner whose city is LA-metro. Not associates, not paralegals.
- Creative/production shop: Founder or Head of Operations. Not the talent (writer, producer, director, editor).

SKIP ENTIRELY if the company is: coworking (Industrious, WeWork, Regus, Convene), real estate developer/landlord (Hudson Pacific, Onni, Brookfield), competing brokerage (CBRE, Cushman, JLL, Colliers, Kidder Mathews, Avison Young, Charles Dunn, Madison Partners, LA Realty Partners, Newmark).

Return ONLY valid JSON. Each pick MUST include reason that mentions the picked person's city.
{"picks": [{"index": 0, "reason": "COO based in Los Angeles — runs the LA office", "confidence": 0.9}]}

If nobody plausible (including the no-LA-head case for 50+ employee multi-office firms), return:
{"picks": [], "_skip_reason": "no_la_office_head_named"}"""


def build_prompt(company: str, domain: str, org_meta: Dict[str, Any], roster: List[Dict[str, Any]]) -> str:
    """Build the user prompt for stakeholder picking on a single company."""
    if not roster:
        return None
    emp = org_meta.get("estimated_num_employees") or "unknown"
    industries = org_meta.get("industries") or []
    industry = org_meta.get("industry") or (
        industries[0] if isinstance(industries, list) and industries else ""
    )
    desc = (org_meta.get("short_description") or "")[:200]
    roster_lines = []
    for i, p in enumerate(roster):
        name = f"{p.get('first_name', '').strip()} {p.get('last_name', '').strip()}".strip()
        title = p.get("title", "") or "(no title)"
        city = p.get("city", "")
        line = f"  {i}. {name} — {title}"
        if city:
            line += f" · {city}"
        roster_lines.append(line)
    return f"""Company: {company}
Domain: {domain}
Employees: {emp}
Industry: {industry}
Description: {desc}

Roster:
{chr(10).join(roster_lines)}

Pick 1-2 best lease decision makers. JSON only."""


def parse_response(text: str, roster: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract picks from a single-company response."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    picks = data.get("picks", [])
    out = []
    for p in picks[:2]:
        idx = p.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(roster):
            continue
        person = roster[idx]
        out.append({
            "apollo_id": person.get("id"),
            "first": person.get("first_name", ""),
            "last": person.get("last_name", ""),
            "title": person.get("title", ""),
            "city": person.get("city", ""),
            "linkedin": person.get("linkedin_url", ""),
            "llm_reason": p.get("reason", ""),
            "llm_confidence": p.get("confidence", 0),
        })
    return out


def batch_prompt(
    batches: List[Dict[str, Any]],
    vacancy: Dict[str, Any] = None,
    outgoing_tenants: List[str] = None,
    biz_mgr_hosts: Dict[str, Dict[str, Any]] = None,
) -> str:
    """Build a single mega-prompt for multiple companies at one building.

    batches: [{"company": str, "domain": str, "org": dict, "roster": list}, ...]
    vacancy: the vacancy dict — adds context block so the LLM knows the building/floor
    outgoing_tenants: company names already filtered as same-suite outgoing
    biz_mgr_hosts: {suite: {count, likely_host}} for client-LLC mail-drop suites

    Sanity-check rules guard against:
      - Wrong-domain Apollo matches (e.g. "G.D.K. Inc" → FINRA)
      - Far-from-vacancy HQ (mail-drop only)
      - Landlord/coworking/brokerage misclassified as tenant
      - Building-LLC names ("11601 Wilshire Co LLC")
      - Personal LLC / shell / registered-agent mail drops
    """
    parts = [SYSTEM, "\n\n---\n\n"]
    parts.append("You will see multiple companies. For EACH, pick 1-2 lease decision makers.\n")
    parts.append('Return ONE JSON object keyed by company name:\n')
    parts.append('{"CompanyA": {"picks": [...]}, "CompanyB": {"picks": [...], "_skip_reason": "..."}}\n\n')
    parts.append("## Sanity checks — flag with _skip_reason and picks:[] if ANY of these apply:\n")
    parts.append("- Apollo description clearly describes a DIFFERENT company than the one named\n")
    parts.append('  (e.g. "G.D.K. Inc" but description talks about FINRA, or "Dempsey Law" LA but description says Oshkosh WI)\n')
    parts.append("- Apollo HQ city is far from the vacancy city (company uses this address for mail only)\n")
    parts.append("- Company name looks like a landlord or asset LLC (e.g. '11601 Wilshire Co LLC', 'Property Holdings')\n")
    parts.append("- Company is a competing brokerage, coworking operator, or the building's property manager\n")
    parts.append("- Tenant is a personal LLC / shell / registered-agent mail drop\n\n")

    if vacancy:
        parts.append("## Vacancy context\n")
        parts.append(f"- Building: {vacancy.get('building_name', '?')}\n")
        parts.append(f"- Address: {vacancy.get('address', '?')}\n")
        parts.append(
            f"- Floor {vacancy.get('floor', '?')}, Suite {vacancy.get('suite', '?')} "
            f"· {vacancy.get('sqft', '?')} SF {vacancy.get('use', '')}\n\n"
        )

    if outgoing_tenants:
        parts.append(f"## Already skipped (same-suite outgoing tenant): {', '.join(outgoing_tenants)}\n\n")

    if biz_mgr_hosts:
        parts.append("## Biz-mgr hosted suites (ignore individual client LLCs registered here):\n")
        for suite, info in biz_mgr_hosts.items():
            parts.append(
                f"- Ste {suite}: {info.get('count', '?')} client LLCs hosted by {info.get('likely_host', '?')}\n"
            )
        parts.append("\n")

    for b in batches:
        parts.append(f"\n### {b['company']}\n")
        single = build_prompt(b["company"], b["domain"], b.get("org", {}), b["roster"])
        if single:
            parts.append(single)
        parts.append("\n")
    return "".join(parts)


def parse_batch_response(text: str, batches: List[Dict[str, Any]]) -> Tuple[Dict[str, list], Dict[str, str]]:
    """Parse per-company picks + sanity-skip reasons.

    Returns (picks_by_company, skip_reasons_by_company).
    """
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}, {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}, {}
    picks_out = {}
    skip_reasons = {}
    for b in batches:
        company = b["company"]
        entry = data.get(company, {}) or {}
        raw_picks = entry.get("picks", [])
        # Resolve indices to roster entries
        resolved = []
        for p in raw_picks[:2]:
            idx = p.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(b["roster"]):
                continue
            person = b["roster"][idx]
            resolved.append({
                "apollo_id": person.get("id"),
                "first": person.get("first_name", ""),
                "last": person.get("last_name", ""),
                "title": person.get("title", ""),
                "city": person.get("city", ""),
                "linkedin": person.get("linkedin_url", ""),
                "llm_reason": p.get("reason", ""),
                "llm_confidence": p.get("confidence", 0),
            })
        picks_out[company] = resolved
        if entry.get("_skip_reason"):
            skip_reasons[company] = entry["_skip_reason"]
    return picks_out, skip_reasons


if __name__ == "__main__":
    # Quick self-test
    sample_roster = [
        {"first_name": "Dean", "last_name": "Horwitz", "title": "Chief Operating Officer", "city": "Menlo Park"},
        {"first_name": "Eric", "last_name": "Harrison", "title": "Co-CEO & Founder", "city": "Palo Alto"},
        {"first_name": "Lauren", "last_name": "Kim", "title": "Managing Director", "city": "Menlo Park"},
    ]
    batches = [{
        "company": "IEQ Capital, LLC",
        "domain": "ieqcapital.com",
        "org": {"estimated_num_employees": 240, "industry": "financial services",
                "short_description": "Independent RIA managing $35B for ultra-HNW families."},
        "roster": sample_roster,
    }]
    print(batch_prompt(batches, vacancy={"building_name": "11601 Wilshire", "address": "11601 Wilshire Blvd, LA",
                                          "floor": 4, "suite": "420", "sqft": 2513, "use": "Office"}))
