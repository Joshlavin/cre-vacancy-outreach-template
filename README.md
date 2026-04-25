# CRE Vacancy Outreach

A nightly Claude Routine that reads CoStar vacancy alerts in your Gmail, finds the neighboring tenants on the floor above and below each vacancy, identifies the right lease decision-maker at each, finds their email, and saves ready-to-send outreach drafts in your Drafts folder. You review and send in the morning.

Built on Anthropic Claude Code Routines. Runs in Anthropic's cloud — your Mac doesn't need to be on. Skill code is fetched fresh from this public repo at the start of every run.

This public repo contains the skill code only — no API keys or personal config. Setup is handled by a separate plugin shipped privately to each broker, which generates a one-paste Routine config with their own keys and signature info baked in.

---

## How install works (broker-side)

The broker receives the plugin from the author privately (zip or private marketplace), installs it in their Claude Code, runs `/cre-setup`, and gets a single pre-built Instructions block to paste into a new Routine. ~90 seconds of clicking, no GitHub account required.

The Instructions block:
1. Clones this public repo into `/tmp/cre` at the start of every run
2. Writes the broker's API keys (Apollo + Firecrawl) into `helpers/.env`
3. Reads `SKILL.md` and runs the daily skill flow

The keys live only in:
- The plugin source the broker installs (private to them)
- The Routine Instructions field on their Anthropic account (private to them)

Never in this public repo.

---

## What the broker sees each morning

Open Gmail → Drafts → search `subject:[CRE]`. Drafts are organized as:

- `[CRE 2026-04-25] Space adjacent to your Suite 420 — 11601 Wilshire` — verified email, ready to send
- `[CRE 2026-04-25] [GUESS] Space adjacent to ...` — email is a pattern guess (review before sending)
- `[CRE 2026-04-25] [NO EMAIL] Acme Corp (Suite 510) — 11601 Wilshire` — no email found, sent to the broker's own inbox
- `[CRE 2026-04-25] [NO TENANTS FOUND] 11601 Wilshire` — discovery returned nothing (rare; usually pure-retail buildings)

A one-line digest also lands in `BROKER_EMAIL` after each run summarizing what was produced.

---

## Editing the email body template

The default email body lives in SKILL.md (Step 7 of Mode: Normal run). The broker can override the template without touching the repo:

1. Create a Google Doc in their Drive (any title).
2. Paste a body template using `{first_name}`, `{building_name}`, `{vacancy_suite}`, `{vacancy_sqft}`, `{relation_phrase}`, `{broker_name}`, `{broker_firm}`, `{broker_phone}`, `{broker_signature_address}` placeholders.
3. Have the plugin author regenerate the Instructions block with `DRAFT_TEMPLATE_DOC_URL=<the Doc URL>` baked in, or add it manually to the existing Instructions.
4. Add **Google Drive** to the Routine's connectors list (alongside Gmail).

The skill fetches the Doc each run and uses it instead of the default. Edit the Doc anytime; next night's drafts use the new wording.

---

## Dedup

Threads we've drafted from get a Gmail label `claude-cre-processed`. The inbox query filters those out next time, so the same alert is never reprocessed.

You can see what's been processed by searching Gmail for `label:claude-cre-processed`.

---

## Switching from Gmail to Outlook later

When the broker changes companies and ends up on Outlook:

1. Open the Routine in Claude Code → Edit.
2. In Connectors, remove **Gmail** and add **Microsoft 365**.
3. Click Connect → Allow on the Microsoft permission page.
4. Save.

The skill detects which connector is present and routes through it. The label name + draft format stay identical.

---

## Configuration reference

Pipeline knobs (max vacancies per run, dedup label name, draft subject prefix, Firecrawl Hobby tier credit limits, etc.) live at the top of `SKILL.md`. Changes take effect on the next nightly run since the Routine clones a fresh copy each time.

---

## Files

- `SKILL.md` — full skill spec; what runs each night
- `helpers/sweep.py` — Firecrawl address-reverse-lookup tenant discovery
- `helpers/discover.py` — LA City Business License tenant discovery (LA proper only)
- `helpers/apollo.py` — Apollo org enrich, roster, by-address, person enrich
- `helpers/emails.py` — Apollo `/people/match` + pattern-guess fallback
- `helpers/stakeholder.py` — LLM stakeholder-picker prompt builder
- `helpers/cache.py` — building tenant cache (intra-run only)
- `helpers/digest.py` — nightly digest builder
- `helpers/search_cache.py` — Firecrawl search-result cache (intra-run only)
- `.env.template` — local-dev reference for the keys helpers expect at runtime
