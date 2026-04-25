---
name: cre-vacancy-outreach
description: Processes CoStar vacancy alerts and saves outreach drafts to neighboring tenants in Gmail Drafts. Triggers on "run cre outreach", "process costar vacancies", or pasted CoStar alert text.
---

# CRE Vacancy Outreach Pipeline

> **Purpose:** Process CoStar vacancy alerts. For each vacancy, find companies on the same floor + one floor above + one floor below. Pick the LA-based lease decision-maker at each company via LLM judgment over a verified Apollo roster. Find their email. Save a ready-to-send draft directly to Gmail Drafts so the broker reviews and sends in one click the next morning.

---

## Configuration

```yaml
# ─── INBOX QUERY ────────────────────────────────────────────────────────────
# Routine scans Gmail for new CoStar alerts. Dedup is via a Gmail label
# `claude-cre-processed` applied after each draft (handled by helpers/processed.py).
# The `-label:` clause filters out anything we've already processed.
inbox_query_gmail: "(from:no-reply@alerts.costar.com OR subject:\"CoStar Daily Alert\" OR subject:\"CoStar Alert\") -label:claude-cre-processed newer_than:8d"

# ─── DEDUP LABEL ────────────────────────────────────────────────────────────
# Created on first run if it doesn't exist (Gmail allows label create via the
# Gmail connector's compose scope). Applied to every source thread we draft from.
dedup_label: "claude-cre-processed"

# ─── DRAFT ORGANIZATION ─────────────────────────────────────────────────────
# Every draft subject is prefixed with [CRE YYYY-MM-DD] so the broker can
# filter Drafts by `subject:[CRE]` (all) or `subject:"[CRE 2026-04-25]"` (one run).
# Optional: broker creates a Gmail filter `subject:[CRE]` → label CRE Outreach
# to get a sidebar label (filter rule lives Gmail-side, no scope needed).
draft_subject_prefix: "[CRE {run_date}]"

# ─── SCHEDULE ───────────────────────────────────────────────────────────────
daily_cron: "0 23 * * *"   # 11pm America/Los_Angeles

# ─── SAFETY CAPS ────────────────────────────────────────────────────────────
max_vacancies_per_run: 8
max_tenants_per_vacancy: 5
max_contacts_per_tenant: 2

# ─── BUILDING CACHE ─────────────────────────────────────────────────────────
# Verified tenant lists cached per (building, floor-range). The Routine
# clones a fresh repo each run into /tmp/cre and has no write-back to the
# template repo, so the cache is INTRA-RUN ONLY — useful when one nightly
# run hits multiple vacancies in the same building (cache hit on the second
# vacancy). Across runs, the cache resets to empty.
#
# Cost of no cross-run persistence: ~60 Firecrawl credits per re-discovered
# building. Hobby tier ($19/mo, 3000 credits) absorbs daily usage for one
# broker. Upgrade to Standard if you cross ~50 vacancies/month.
cache_dir: "cache/buildings"
cache_ttl_days: 60

# ─── BROKER SIGNATURE ───────────────────────────────────────────────────────
# All broker identity comes from Routine env vars at runtime. The template
# repo is identical for every install — personalization happens in the user's
# Routine config (claude.ai/code/routines → Edit → Environment variables).
#
# Required env vars (set on the Routine):
#   BROKER_NAME                — e.g. "Kurt Davis"
#   BROKER_FIRM                — e.g. "The Irvine Company"
#   BROKER_SUBMARKET           — e.g. "West LA / Century City / DTLA"
#   BROKER_PHONE               — e.g. "+1 310-555-1234"        (omit signature line if blank)
#   BROKER_SIGNATURE_ADDRESS   — e.g. "100 Innovation, Irvine CA 92617"  (omit if blank)
#   BROKER_EMAIL               — broker's own Gmail; used as digest TO and as
#                                safety TO for [NO EMAIL] / [NO TENANTS FOUND] drafts

# ─── WEEKLY DIGEST ──────────────────────────────────────────────────────────
# After each run, send a one-line summary email to broker_email so the broker
# knows the cron actually ran and what was produced. Closes the feedback loop.
send_digest: true

# ─── EXECUTION ENVIRONMENT ──────────────────────────────────────────────────
# This skill runs inside an Anthropic Routine on the broker's claude.ai
# (Pro+) account. The Routine is created via Claude Code (desktop) with
# Repository = "Default" — no GitHub fork required. The Instructions field
# clones this template repo into /tmp/cre at the start of every run via:
#
#   git clone --depth 1 https://github.com/Joshlavin/cre-vacancy-outreach-template.git /tmp/cre
#
# Schedule: 11pm America/Los_Angeles. No local fallback.

# ─── API KEYS — INLINED IN ROUTINE INSTRUCTIONS ─────────────────────────────
# Default routines don't expose env-var fields. Instead, the broker's API
# keys + signature config get baked directly into the Routine's Instructions
# block, generated once by /cre-setup (which has the keys hardcoded in the
# plugin source the broker installs).
#
# This is safe because:
#   - The Routine Instructions field is private to the broker's Anthropic
#     account (only they can see or edit it).
#   - The plugin source containing the hardcoded keys is shipped privately
#     to the broker — never in the public template repo.
#
# At runtime the Bash bootstrap step writes the keys into /tmp/cre/helpers/.env
# (which the existing helper modules already read via their _load_env() logic).
#
# Where to get the keys (when Josh first hardcodes them into a copy of this
# plugin for a new broker):
#   APOLLO_API_KEY     — https://app.apollo.io/#/settings/integrations/api
#   FIRECRAWL_API_KEY  — https://www.firecrawl.dev/app/api-keys
#                        Hobby tier ($19/month, 3000 credits) is enough for
#                        ~50 vacancies/month.
```

---

## Why this pipeline (one paragraph)

The single highest-leverage CRE outreach play is contacting the tenant on the floor above or below a brand-new vacancy the day it lists — they're 10× more likely to want that space than a cold prospect. The hard part is L3: finding the actual neighbors. We use **address-reverse-lookup sweeps via Firecrawl** (works for any building in any city) as the primary source, **LA City Business License data** (highest recall for LA-proper buildings, ~50 tenants per Class-A) as a supplementary signal, and **Apollo orgs-by-address** + **building-website scrapes** as fallbacks. For each verified neighbor, we pull a 10-person Apollo roster and let Claude pick the 1–2 lease decision-makers using titles + company size + sanity checks. We then enrich the picks (full name + city) and resolve verified emails via Apollo `/people/match`.

---

## Trigger modes

**Mode A — Manual.** User pastes a CoStar alert (or any text with address + suite + floor) and says "run cre outreach on this." Parse vacancies from the pasted text. Skip the inbox scan. Skip the digest at the end (manual runs don't get a digest — the user already sees what happened).

**Mode B — Auto (Routine).** The 11pm Anthropic Routine searches Gmail for new CoStar alerts not yet labeled `claude-cre-processed`, processes them, drops drafts in Gmail Drafts, applies the dedup label to each source thread, and emails a one-line digest to the broker.

---

## Pre-flight check (before every run)

Silently verify:

1. **Gmail connector** — connector tools available (`mcp__*__search_threads`, `mcp__*__create_draft`, `mcp__*__create_label` or equivalent label-modify capability).
2. **Apollo key loaded** — `python3 helpers/apollo.py org --domain salesforce.com` returns a payload with a `name` field. If not, log and exit (no user to prompt in a Routine run).
3. **Firecrawl key loaded** — env var `FIRECRAWL_API_KEY` non-empty. The sweep helper will fail loud if it's missing.
4. **Broker env vars present** — `BROKER_NAME` and `BROKER_EMAIL` non-empty. If missing, write `cache/runs/{date}-failed.md` noting "broker env vars not set on Routine" and exit.
5. **Dedup label exists** — list Gmail labels; if `claude-cre-processed` is missing, create it. Idempotent.

If any check fails on a Routine run: write `cache/runs/{date}-failed.md` with the failure and exit. Don't prompt.

---

## Mode: Setup (first run)

**Trigger:** `/cre-setup` — typically run once after the broker installs the plugin in their local Claude Code or Cowork session.

The setup wizard's job: walk the broker through creating a **Default-repository Routine** in Claude Code. No GitHub fork, no Claude GitHub App install, no copy-pasting cron expressions. The broker pastes one prepared Instructions block, sets one connector and a handful of env vars, clicks Create.

The Routine clones this template repo's public copy at the start of every run via `git clone --depth 1`. No write access back to the repo is needed — dedup state lives in a Gmail label, run logs are ephemeral, and the building cache rebuilds on cache miss (costs ~60 Firecrawl credits per re-discovery; acceptable on Hobby tier for daily-volume usage).

### Step 1: Greet + check what's already done

If a recent run log exists in Gmail (search `subject:"CRE Vacancy Outreach digest"` for the last 7 days), the Routine is already wired up. Skip the wizard and offer `/cre-run` as a smoke test against the most recent CoStar alert.

Otherwise: proceed.

### Step 2: Capture broker signature + remind about API keys

Use `AskUserQuestion` (or an elicitation form) to collect the six broker fields:

- `BROKER_NAME` (e.g. "Kurt Davis")
- `BROKER_FIRM` (e.g. "The Irvine Company")
- `BROKER_SUBMARKET` (e.g. "West LA / Century City / DTLA")
- `BROKER_PHONE` (optional — omit signature line if blank)
- `BROKER_SIGNATURE_ADDRESS` (optional — omit signature line if blank)
- `BROKER_EMAIL` (broker's own Gmail; gets the digest each run)

Then remind the broker they'll also need API keys for two services:
- **Apollo** — https://app.apollo.io/#/settings/integrations/api → "Create new key" → copy the string
- **Firecrawl** — https://www.firecrawl.dev/app/api-keys → Hobby tier ($19/mo, 3000 credits) is enough → copy the key

Hold all eight values in memory — they get pasted into the Routine form in Step 4.

### Step 3: Walk the broker through opening the Routine creator

Tell the broker:

> Open Claude Code (the desktop app, not the website). In the sidebar, click **Routines** → **New Routine**.

Wait for them to confirm they see the empty form with fields for Name, Instructions, Trigger, and Connectors.

### Step 4: Hand them the paste blocks

The Routine form has four blocks the broker fills in. Output these to chat in code-block format so they can copy each one cleanly:

**Block 1 — Name:**
```
CRE Vacancy Outreach
```

**Block 2 — Instructions** (paste verbatim into the big Instructions textarea, with `{APOLLO_KEY}`, `{FIRECRAWL_KEY}`, and the BROKER_* values substituted from the plugin's hardcoded config):

```
You are running on Anthropic's cloud as a scheduled CRE outreach Routine.

# Broker config (use these values when personalizing draft signatures)
broker_name: {BROKER_NAME}
broker_firm: {BROKER_FIRM}
broker_submarket: {BROKER_SUBMARKET}
broker_phone: {BROKER_PHONE}
broker_signature_address: {BROKER_SIGNATURE_ADDRESS}
broker_email: {BROKER_EMAIL}

# Step 1 — bootstrap skill code + write API keys
Run this in Bash:

  cd /tmp && rm -rf cre && \
  git clone --depth 1 https://github.com/Joshlavin/cre-vacancy-outreach-template.git cre && \
  cd cre && \
  cat > helpers/.env <<'ENV_EOF'
  APOLLO_API_KEY={APOLLO_KEY}
  FIRECRAWL_API_KEY={FIRECRAWL_KEY}
  ENV_EOF

# Step 2 — execute the skill
Read /tmp/cre/SKILL.md and follow "Mode: Normal run" end-to-end (Steps 1–12). Trigger Mode B (cron) applies — scan Gmail using the configured inbox_query_gmail, skip threads already labeled `claude-cre-processed`, cap at max_vacancies_per_run.

The Python helpers (helpers/apollo.py, helpers/sweep.py, helpers/emails.py) automatically read APOLLO_API_KEY and FIRECRAWL_API_KEY from helpers/.env via their _load_env() logic — no extra exporting needed.

Use the broker config above when generating draft signatures.

Use the Gmail connector for everything Gmail-related: search threads, create drafts, apply the `claude-cre-processed` label after drafting from a thread, send the digest to {BROKER_EMAIL}.

Do not attempt `git push`. cache/buildings/ and cache/runs/ inside /tmp/cre are ephemeral; they don't need to persist across runs. Dedup state lives in the Gmail label.

If pre-flight checks fail, write a one-line summary to a draft email titled "[CRE FAILED]" addressed to {BROKER_EMAIL} and exit. Do not retry.
```

**Block 3 — Trigger:** select **Daily** in the trigger picker, set the time to **11:00 PM**. Confirm the timezone matches **America/Los_Angeles**.

**Block 4 — Connectors:** ensure **Gmail** is in the connectors list. Remove any connectors you don't want this Routine to access (only Gmail is required).

**Block 5 — Repository:** click the "Select a repository" dropdown and pick **Default**. (Do NOT select a GitHub repo — the prompt above clones what it needs at runtime.)

Then say: "Click **Create**." No env vars or Permissions tab needed — the Instructions block above contains everything.

### Step 5: Smoke test + confirm

Tell the broker: "From the Routine detail page, click **Run now** at the top. Watch the run log appear — you should see Bash clone the repo, then the skill execute. After 30–90 seconds, check your Gmail Drafts folder for any new `[CRE …]` drafts (or a digest with 'no new alerts' if your inbox didn't have any unprocessed CoStar emails)."

Once they confirm a successful run, show:

> You're set. Every night at 11pm Pacific the Routine will scan your Gmail for new CoStar alerts, find neighbor tenants on the same floor and floors above/below each vacancy, pick the right decision-maker, and save ready-to-send drafts in your Drafts folder. Search Drafts for `subject:[CRE]` to see them all. You'll get a digest summary at {BROKER_EMAIL} after each run.
>
> Runs on Anthropic's servers — your Mac doesn't need to be on.

### Optional: editable email template via Google Doc

If the broker wants to edit the email body without me pushing a code change, they can:

1. Create a Google Doc in their Drive (any title).
2. Paste a body template using `{first_name}`, `{building_name}`, `{vacancy_suite}`, `{vacancy_sqft}`, `{relation_phrase}`, `{broker_name}`, `{broker_firm}`, `{broker_phone}`, `{broker_signature_address}` placeholders.
3. Copy the Doc URL and add it to the Routine's env vars as `DRAFT_TEMPLATE_DOC_URL`.
4. Add **Google Drive** to the Routine's connectors list (alongside Gmail).

Step 7 of Mode: Normal run will fetch the Doc each run via the Drive connector and use it instead of the hardcoded template. If the env var is unset, the default template is used.

---

## Mode: Normal run

### Step 1: Get vacancy input

**Manual paste:** parse vacancies from pasted text. Skip inbox.

**Routine (cron):** call Gmail `search_threads` with `inbox_query_gmail`. The query already includes `-label:claude-cre-processed`, so any thread we've drafted from before is filtered out at search time — no per-thread is-processed check needed.

If zero new threads: log `cache/runs/{date}-empty.md` and exit. Optionally still send digest with "no new alerts this week" if `send_digest: true`.

Cap at `max_vacancies_per_run`.

### Step 2: Parse vacancies

Per listing in the alert body, extract:

```json
{
  "building_name": "...",
  "address": "11601 Wilshire Blvd, Los Angeles, CA 90025",
  "floor": 4,
  "suite": "420",
  "sqft": 2513,
  "use": "Office",
  "rent": "$3.59 - 4.39 FS",
  "source_thread": "19d7d0d525a226f5"
}
```

Group by building — run discovery once per building, draft per vacancy suite. Skip anything missing address + suite (note in run log).

### Step 3: Cache check (per building)

```bash
python3 helpers/cache.py get --building "{address}" --floors {min_floor},{max_floor}
```

- **Cache hit (within `cache_ttl_days`):** use cached tenant list. Skip discovery. Saves the tokens and Apollo credits that go to research.
- **Cache miss:** continue to step 4. Write the verified result back to cache after step 6.

### Step 4: Tenant discovery (two sources, merged)

**Source A — Address-reverse-lookup sweep (PRIMARY).** Enumerates plausible suite addresses on adjacent floors and reverse-looks-up each via Firecrawl Search. Works for any building in any city — no dependency on tenant directories.

```bash
python3 helpers/sweep.py "{address}" --suite {vacancy_suite} --step 10 --max-queries 30
```

Returns `{tenants: [{company, suite, floor, relation, evidence_url, confidence}]}`. Cost: ~30-60 Firecrawl credits per vacancy.

**Source B — LA City Business License data (SUPPLEMENTARY).** Stdlib only, free, fast. Catches small businesses registered in LA City proper that don't have web presence. Useless for non-LA cities (Beverly Hills, Santa Monica) — skip the call there.

```bash
python3 helpers/discover.py "{address}" --suite {vacancy_suite} --top 12
```

Only run this if the address is in `Los Angeles, CA` (city == "Los Angeles"). For Beverly Hills, Santa Monica, Pasadena, Culver City, etc., skip.

**Merge both sources.** Dedupe by (suite, normalized_company_name). When the same suite appears in both sources with different company names, keep both — they may be co-tenants. Cap at `max_tenants_per_vacancy` (default 5) by sorting on confidence descending.

**If both sources return 0** (rare; happens for pure retail vacancies and remote-area buildings):

1. **Building's official site:** `WebSearch '"{building_name}" tenant directory site'`. If a non-aggregator URL returns, fetch with Firecrawl on `/tenants`, `/directory`, `/about`. Extract tenants from markdown via Claude.
2. **Apollo orgs-by-address:** `python3 helpers/apollo.py by-address --address "{address}"`. Often returns 0 (Apollo's address indexing is spotty), but credit-cheap to try.
3. **If still empty:** save a placeholder draft `[NO TENANTS FOUND] {building_name}` to broker_email so they see the building was attempted.

### Step 5: Stakeholder picking (LLM, no API call)

For each ranked tenant (top N):

1. **Get domain.** If the discovery source already provided a `domain` field, use it. Otherwise, web-search `"{company} site:linkedin.com OR official website"` to resolve a domain. If no domain resolves after 1 query, skip with reason `no_domain_resolved`.

2. **Apollo org enrich** to get company size + industry + description:
   ```bash
   python3 helpers/apollo.py org --domain {domain}
   ```

3. **Apollo roster** — pull 10 senior-tier people:
   ```bash
   python3 helpers/apollo.py roster --domain {domain} --limit 10
   ```
   Last names are obfuscated at this tier (`Hi***h`); that's fine — the picker selects on title + first name.

4. **Build the stakeholder prompt** by collecting batches across all tenants at this building, then call `helpers/stakeholder.py`'s `batch_prompt()` to produce one mega-prompt covering all companies. The prompt has:
   - The decision-maker rules by company size (solo / small / mid / enterprise)
   - The skip list (coworking, landlords, brokerages)
   - Sanity checks (wrong-domain Apollo match, far-from-vacancy HQ, building-LLC names)
   - Per-company: name, domain, employee count, industry, description, roster

5. **Read and judge.** As the Claude session running this SKILL: read the prompt, output JSON keyed by company name with `picks` (max 2 per company) or `_skip_reason`. Use the rules in the prompt strictly — for enterprise (200+) companies without a named LA office head, skip rather than guess.

6. **Parse the response** with `parse_batch_response()` to get picks-by-company.

### Step 6: Enrich picked stakeholders + find emails

For each picked person (max 2 per company):

```bash
python3 helpers/apollo.py enrich --id {apollo_id}
```

Returns full name (last_name no longer obfuscated), city, LinkedIn URL, organization confirmation. Costs 1 Apollo credit per call.

Then:

```bash
python3 helpers/emails.py --names-json '[{"first":"{first}","last":"{last}","domain":"{domain}"}]'
```

Returns:
- `email` + `method: "apollo"` → verified, ready to send
- `email` + `method: "pattern_guess"` + `apollo_title` → unverified guess; subject prefix `[GUESS]`
- `email: null` + `method: "not_found"` → no email at all; safety draft to broker_email with `[NO EMAIL]` prefix

### Step 7: Build drafts

Subject:
```
[CRE {run_date}] Space adjacent to your {neighbor_suite} — {building_name}
```

If guess: `[CRE {run_date}] [GUESS] Space adjacent to your {neighbor_suite} — {building_name}`
If no email: `[CRE {run_date}] [NO EMAIL] {company} ({neighbor_suite}) — {building_name}`

Body:
```
Hey {first_name},

{broker_name} with {broker_firm}. I handle office tenant representation in {broker_submarket}, and while we have not had the chance to connect, I wanted to reach out with something timely.

A suite {relation_phrase} at {building_name} just hit the market — Suite {vacancy_suite}, {vacancy_sqft} RSF. My team has worked with a few other groups in and around your building, and figured this was worth passing along.

If you have any needs for expansion or restructuring your layout, this could be a unique opportunity to leverage.

If valuable to you, I would be happy to set up a quick 15 minute call or meeting, or send over additional info on where your building is currently executing lease deals.

Best,
{broker_name}
{broker_firm}
{broker_phone}
{broker_signature_address}
```

Omit any signature line that's empty in config.

`relation_phrase` (computed from proximity rank):
- `right next door` → "right next door to yours"
- `adjacent same floor` → "a few doors down from yours on the same floor"
- `same floor` → "on the same floor as yours"
- `floor above` → "on the floor right above yours"
- `floor below` → "on the floor right below yours"

### Step 8: Save drafts

For each (contact × vacancy):

```
mcp__<gmail-connector>__create_draft(
  to=[contact.email or broker_email if no_email],
  subject=<built above>,
  body=<built above>,
)
```

That's it — no label apply, no thread modification. Subject prefix handles organization.

### Step 9: Mark source threads processed

For each Gmail thread we processed (Routine mode only):

```
mcp__<gmail-connector>__modify_thread(
  thread_id={thread_id},
  add_label_ids=[<id_of_claude-cre-processed>]
)
```

(Exact tool name varies by connector — could be `modify_thread`, `apply_label`, or `update_thread_labels`. Apollo agent: pick whichever your connector exposes; the inbox query in Step 1 has `-label:claude-cre-processed`, so any thread carrying that label won't appear in tomorrow's search.)

This replaces the previous local JSON cache approach — labels are server-side, persist across runs, and survive a fresh Routine clone.

### Step 10: Cache the verified neighbor list

```bash
echo '<json of verified tenant list>' | python3 helpers/cache.py put --building "{address}" --floors {min_floor},{max_floor}
```

So the next vacancy at this building (could be days or weeks later) skips discovery.

### Step 11: Log the run

Write `cache/runs/{date}.md` with:
- Vacancies processed (count + addresses + source thread IDs)
- Buildings researched (count, cache hits vs cold runs)
- Drafts created (count, status breakdown: verified / guess / no_email)
- Per-vacancy: which tenants surfaced, which got LLM-skipped (and why), which got drafts
- Wall-clock + Apollo credits consumed (rough estimate: 1 per `roster` + 1 per `enrich` + 1 per `/people/match`)
- Any failures with reason

### Step 12: Send digest (cron mode + send_digest=true)

```bash
python3 helpers/digest.py --run-log "cache/runs/{date}.md" --broker-name "{broker_name}" --run-date {date}
```

Returns `{subject, body_plaintext, body_html, counts}`. Then:

```
mcp__<gmail-connector>__create_draft(
  to=[broker_email],
  subject=<digest subject>,
  body=<digest plaintext>,
  htmlBody=<digest html>,
)
```

The digest is a draft, not a send — broker reviews and discards or self-sends. The point is presence in Drafts as a Monday morning signal that the cron ran.

---

## Hard rules

1. **Never send emails** — drafts only.
2. **Never fabricate decision makers** — Apollo enrich must confirm full name + city before drafting; skip otherwise.
3. **Respect skip list** — coworking, landlords, brokerages (current SKIP list embedded in `helpers/stakeholder.py` SYSTEM prompt).
4. **Never reprocess** a thread already labeled `claude-cre-processed` in Gmail.
5. **Cap at `max_vacancies_per_run`** (default 8).
6. **Don't hallucinate broker info** — omit empty signature lines.
7. **Cache before research** — always check building cache before running discovery.
8. **Main session for research** — don't dispatch sub-agents for discovery (sub-agents have unreliable web/Apollo access; main session inherits the user's connector tokens).

---

## Edge cases

- **10+ vacancies in one alert:** process up to cap, log skipped.
- **Duplicate vacancy across alerts:** dedupe by `(address, suite)`. Cache check covers same-building re-runs.
- **Non-LA building** (Beverly Hills, Pasadena, Culver City): LA City helper returns nothing → fall through to building-site scrape + suite-number sweep.
- **Outgoing tenant at vacancy suite:** discover.py auto-excludes via `same_suite_outgoing`. No drafts to that company.
- **Empty discovery result for a building:** save one placeholder draft to broker_email with `[NO TENANTS FOUND]` subject so the broker sees the building was attempted and can manually research.
- **Apollo daily credit limit hit:** log per-vacancy how many calls succeeded; remaining vacancies fall through to `no_email` placeholder drafts.

---

## Files

- `SKILL.md` — this file
- `helpers/sweep.py` — Address-reverse-lookup tenant discovery via Firecrawl Search (PRIMARY source — works on any building in any city)
- `helpers/discover.py` — LA City Business License tenant discovery, mail-drop detection, proximity ranking (SUPPLEMENTARY — LA City addresses only)
- `helpers/apollo.py` — Apollo org enrich, roster, by-address search, person enrich
- `helpers/emails.py` — Apollo `/people/match` + Firecrawl team-page scrape + pattern-guess fallback
- `helpers/stakeholder.py` — LLM stakeholder-picker prompt builder + JSON parser
- `helpers/cache.py` — building tenant cache (60-day TTL)
- `helpers/digest.py` — weekly summary email builder
- `helpers/search_cache.py` — Firecrawl search-result cache
- `.env.template` — APOLLO_API_KEY + FIRECRAWL_API_KEY template (only used for local dev; the Routine reads env vars directly)
- `cache/runs/{date}.md` — per-run log written by step 11
- `cache/buildings/<sha1>.json` — written by `helpers/cache.py put`

Note: `helpers/processed.py` and `cache/processed-threads.json` were removed — dedup now lives in the Gmail label `claude-cre-processed` (see Configuration → DEDUP LABEL).
