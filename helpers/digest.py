#!/usr/bin/env python3
"""Build the weekly digest summary.

Called at the end of each cron run. Outputs subject + body for an email
the SKILL sends to the broker, summarizing what got produced this week.

Why: closes the feedback loop. Without it, neither the broker nor we
know whether the cron is actually generating useful output week-over-week.

Usage:
  python3 helpers/digest.py --run-log <path-to-run-log.md>

Stdout JSON:
  {"subject": "...", "body_plaintext": "...", "body_html": "..."}
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path


def parse_run_log(md_text):
    """Extract counts from a run-log markdown file.

    Run logs are written by SKILL.md step 10. They follow a known structure:
      - Vacancies parsed (table)
      - Verified neighbor companies: N
      - Drafts created: N
      - Drafts requiring follow-up: N
    This parser tolerates missing sections.
    """
    counts = {
        "vacancies_parsed": 0,
        "buildings_processed": 0,
        "drafts_verified": 0,
        "drafts_guess": 0,
        "drafts_no_email": 0,
        "drafts_needs_review": 0,
        "drafts_total": 0,
        "skipped": 0,
    }

    for pattern, key in [
        (r"Vacancies (?:processed|parsed):\s*(\d+)", "vacancies_parsed"),
        (r"Buildings researched:\s*(\d+)", "buildings_processed"),
        (r"Drafts created \(verified email\):\s*(\d+)", "drafts_verified"),
        (r"Drafts.*\bguess\b.*?(\d+)", "drafts_guess"),
        (r"Drafts.*no.email.*?(\d+)", "drafts_no_email"),
        (r"requiring follow.?up.*?(\d+)|needs_review.*?(\d+)", "drafts_needs_review"),
    ]:
        m = re.search(pattern, md_text, re.IGNORECASE)
        if m:
            grp = next((g for g in m.groups() if g), "0")
            counts[key] = int(grp)

    counts["drafts_total"] = (
        counts["drafts_verified"] + counts["drafts_guess"] + counts["drafts_no_email"]
    )
    return counts


def build_digest(run_log_path, broker_name=None, drafts_url=None, run_date=None):
    """Build digest subject + body from a run log markdown file."""
    if not run_date:
        run_date = dt.date.today().strftime("%Y-%m-%d")

    if isinstance(run_log_path, (str, Path)) and Path(run_log_path).exists():
        md = Path(run_log_path).read_text()
        counts = parse_run_log(md)
    else:
        md = ""
        counts = {
            "vacancies_parsed": 0, "buildings_processed": 0,
            "drafts_verified": 0, "drafts_guess": 0, "drafts_no_email": 0,
            "drafts_needs_review": 0, "drafts_total": 0, "skipped": 0,
        }

    greeting = f"Hi {broker_name.split()[0]}" if broker_name else "Hi"

    if counts["drafts_total"] == 0:
        subject = f"[CRE digest {run_date}] No new drafts this week"
        body = (
            f"{greeting},\n\n"
            f"This week's CoStar scan ran but didn't produce any sendable drafts.\n\n"
            f"  Vacancies parsed: {counts['vacancies_parsed']}\n"
            f"  Buildings researched: {counts['buildings_processed']}\n"
            f"  Drafts requiring follow-up research: {counts['drafts_needs_review']}\n\n"
            "Most common reasons for an empty week: no new CoStar alerts arrived, "
            "or the buildings in this week's alerts have tenants we couldn't surface "
            "from public sources (Beverly Hills, retail, small offices).\n\n"
            "— CRE Outreach pipeline\n"
        )
    else:
        subject = f"[CRE digest {run_date}] {counts['drafts_total']} drafts ready to send"
        verified_pct = (
            int(100 * counts["drafts_verified"] / counts["drafts_total"])
            if counts["drafts_total"] else 0
        )
        body = (
            f"{greeting},\n\n"
            f"This week's CRE Outreach run is in your Drafts folder.\n\n"
            f"  Vacancies processed: {counts['vacancies_parsed']}\n"
            f"  Buildings researched: {counts['buildings_processed']}\n"
            f"  Drafts created: {counts['drafts_total']}\n"
            f"    • Verified emails (Apollo): {counts['drafts_verified']} ({verified_pct}%)\n"
            f"    • Pattern-guess emails (double-check before send): {counts['drafts_guess']}\n"
            f"    • No-email placeholders (manual research needed): {counts['drafts_no_email']}\n\n"
            f"To find them: open Gmail and search Drafts for `subject:[CRE {run_date}]`.\n"
        )
        if drafts_url:
            body += f"\n  {drafts_url}\n"
        body += (
            "\nReply to this email with any feedback — wrong contact, bad subject, "
            "missed building. We'll tune the pipeline.\n\n"
            "— CRE Outreach pipeline\n"
        )

    body_html = (
        "<p>" + body.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    )

    return {
        "subject": subject,
        "body_plaintext": body,
        "body_html": body_html,
        "counts": counts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-log", required=True)
    ap.add_argument("--broker-name", default=None)
    ap.add_argument("--drafts-url", default=None)
    ap.add_argument("--run-date", default=None)
    args = ap.parse_args()

    result = build_digest(
        args.run_log, args.broker_name, args.drafts_url, args.run_date,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
