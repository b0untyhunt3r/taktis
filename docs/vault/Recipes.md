---
title: Recipes
tags: [recipes, scheduled, patterns, moc]
---

# Scheduled-Pipeline Recipes

A curated catalog of pipelines worth running on a [[Cron Scheduler|schedule]]. Every recipe is buildable today from the existing [[Node Types|14 node types]] and existing [[Expert System|expert personas]] — no new primitives required. Each entry names the trigger cadence, the pipeline shape, the artifact written, and the reason it deserves orchestration instead of a one-shot prompt.

Reminder: scheduled flows are **headless**. `human_gate` and interactive `agent` nodes are rejected by `detect_interactive_nodes` (see [[Cron Scheduler]]). If you need approval, surface it as a written report a human reads on Monday — not a blocking gate at 3 AM.

## Engineering & DevOps

- **PR Controller** — *daily.* Fetches open PRs (`api_call`), `fan_out` over them, an `llm_router` classifies the diff and routes to `database-optimizer`, `security-engineer`, `performance-benchmarker`, or `accessibility-auditor`, then `aggregator` + `file_writer` drops a dated triage report. Ships as [`pr-controller.json`](../../taktis/defaults/pipeline_templates/pr-controller.json) — see [[Pipeline Factory]] seeded templates. Why orchestrated: parallel specialist review at human pace, every morning.
- **Flaky Test Detective** — *daily.* `api_call` pulls the last 50 CI runs, an `agent` (persona `qa-lead`) clusters failure signatures, a `loop` retries until clusters stabilise, `file_writer` writes `flaky/YYYY-MM-DD.md`. Why orchestrated: flakes look random one run at a time and obvious across fifty.
- **Dependency Drift Auditor** — *weekly.* `api_call` to npm/PyPI, `fan_out` per package, `security-engineer` scores CVE exposure, `aggregator` ranks by blast radius, `file_writer` produces an upgrade plan. Why orchestrated: per-package judgement composed across the whole tree.
- **Incident Postmortem Drafter** — *daily, gated by conditional.* `api_call` to PagerDuty, a `conditional` short-circuits when the day was quiet, otherwise three parallel agents (`incident-response-commander`, `sre-site-reliability-engineer`, `threat-detection-engineer`) draft cause/timeline/lessons, `aggregator` merges to `postmortems/`. Why orchestrated: three viewpoints in one pass while the timeline is still fresh.
- **Self-Designing Pipeline Factory** — *weekly.* An `agent` reads `.taktis/wishlist.md`, an `output_parser` splits one wish into a spec, `pipeline_generator` materialises a new template into the DB. The vault's recursive primitive — see [[Pipeline Factory]]. Why orchestrated: pipelines that breed pipelines without you opening the designer.
- **Infra Cost Sentinel** — *daily.* `api_call` to billing API, `text_transform` extracts JSON, `agent` (`finance-tracker`) compares against a baseline embedded in `.taktis/`, `conditional` drops a "no anomalies" line on quiet days and a written explainer otherwise. Why orchestrated: anomaly framing is the value, not the number.
- **SLO Burn-Rate Responder** — *hourly.* `api_call` to Prometheus, `llm_router` classifies burn shape (slow/fast/blameless), three branches each call a different reviewer (`sre-site-reliability-engineer`, `reviewer-general`, `devops`), `file_writer` writes `slo/HH.md` only on non-green. Why orchestrated: the routing IS the response — no human reads green dashboards.
- **Doc Rot Killer** — *weekly.* `fan_out` over `docs/**/*.md`, `agent` (`docs-writer-general`) scores against the linked source files in `.taktis/manifest`, `aggregator` ranks rot, `file_writer` emits a fix list. Why orchestrated: 200-page audits no human will do by hand.

## Business Intelligence

- **Pricing-Page Diff Watcher** — *daily.* `api_call` fetches competitor pricing pages, `text_transform` extracts diffs from the previous snapshot in `.taktis/`, an `agent` (`deal-strategist`) interprets the move, `file_writer` writes `pricing/YYYY-MM-DD.md`. Why orchestrated: a diff is data; an interpretation is intel.
- **Hiring-Intent Radar** — *weekly.* `api_call` over careers pages, `fan_out` per company, `agent` (`recruitment-specialist`) tags signal (new product line, geo expansion), `aggregator` ranks. Why orchestrated: hiring leaks roadmap weeks before the announcement.
- **Stripe Churn Classifier** — *daily.* `api_call` to Stripe events, `llm_router` segments cancels into pricing/product/competitor/dormant, four parallel agents draft an outreach note each, `file_writer` writes `churn/YYYY-MM-DD/` per-segment. Why orchestrated: triage + draft in one window so CS opens Monday with replies queued.
- **EDGAR/Crunchbase Partnership Miner** — *weekly.* `api_call` to EDGAR 8-K and Crunchbase deltas, `fan_out` per filing, `agent` extracts named partnerships, `aggregator` builds a graph of who-just-met-whom, `file_writer` to `intel/partnerships.md`. Why orchestrated: cross-source link discovery a single agent's context window can't hold.
- **Regulatory-Drift Sentinel** — *daily.* `api_call` over Federal Register / EUR-Lex feeds, `agent` (`legal-compliance-checker`) flags items touching your domain (taxonomy in `.taktis/scope.md`), `conditional` writes a brief only when something matched. Why orchestrated: most days nothing matters; when something matters you want it in writing.
- **Narrative-Shift Detector** — *weekly.* `api_call` over a watchlist of analyst feeds and sub-reddits, `agent` (`narratologist`) compares this week's framing against last week's stored in `.taktis/`, `file_writer` writes `narrative/week-NN.md`. Why orchestrated: drift is invisible day-to-day and obvious week over week.
- **Paid-Media Leak Audit** — *daily.* `api_call` for ad spend by channel, `agent` (`paid-media-auditor`) checks against `search-query-analyst` keyword report from last week, `aggregator` lists waste candidates. Why orchestrated: two specialist lenses on the same data, no human sequencing.
- **Vendor-Risk Monitor** — *daily.* `fan_out` across critical vendors in `.taktis/vendors.md`, parallel `api_call` to status pages + breach feeds, `agent` (`compliance-auditor`) scores each, `aggregator` writes `vendors/YYYY-MM-DD.md`. Why orchestrated: per-vendor drilldowns at fleet scale.

## Knowledge Work

- **Vault Rot Detector** — *weekly.* `fan_out` across vault notes, `agent` (`technical-writer`) checks whether each note's `file:line` citations still resolve to the cited code, `file_writer` writes a stale-citation list. Why orchestrated: the doc-vs-code divergence problem this very vault has.
- **Adaptive Spaced-Repetition Curriculum** — *daily.* `api_call` to Anki/Mochi review history, `agent` (`psychologist`) reads the lapse pattern, `agent` (`docs-writer-general`) writes tomorrow's targeted reading list to `study/YYYY-MM-DD.md`. Why orchestrated: tomorrow's deck depends on today's misses.
- **Long-Running Book Research Spine** — *weekly.* An `agent` reads `.taktis/book/outline.md`, an `output_parser` splits the next chapter brief, `fan_out` to four researcher personas (`historian`, `geographer`, `anthropologist`, `psychologist`), `aggregator` writes `book/chapter-N-research.md`. Why orchestrated: a book takes a year; the system needs to keep its place.
- **Personal CRM Drift Watch** — *weekly.* `api_call` to your contact graph, `agent` flags people you haven't pinged in N weeks weighted by closeness in `.taktis/relationships.md`, `file_writer` writes a Sunday-evening reach-out list. Why orchestrated: relationships rot quietly; this is the surfacing layer.
- **Substack Echo-Chamber Audit** — *weekly.* `api_call` over your subscriptions' RSS, `agent` (`narratologist`) checks topic and stance overlap, `file_writer` writes a diversity score and three suggested unsubscribes / new subs. Why orchestrated: filter-bubbles are invisible from inside.
- **ArXiv/PubMed Idea-Collision Engine** — *daily.* `api_call` over yesterday's preprints in two fields you care about (config'd in `.taktis/`), `agent` (`reality-checker`) hunts for cross-field collisions, `conditional` writes `collisions/YYYY-MM-DD.md` only if any landed. Why orchestrated: the value is the surprise — silence on quiet days is a feature.
- **Daily Writing Prompt with Adversarial Panel** — *daily.* `agent` proposes a prompt, `fan_out` to three personas (`narratologist`, `psychologist`, `reviewer-general`) each writing 200 words, `aggregator` stitches into one piece, `file_writer` to `journal/YYYY-MM-DD.md`. Why orchestrated: a panel beats a monologue, every day.
- **Are.na Channel Self-Curator** — *weekly.* `api_call` to your Are.na channel, `agent` (`brand-guardian`) reads channel telos, `agent` (`reality-checker`) flags blocks that drifted off-thesis, `file_writer` writes a prune list. Why orchestrated: collections grow incoherent; the audit is the maintenance.

## Strange & Surprising

The category exists because not every useful pipeline is operational. Two ship in the repo:

- **The Slow Roman Historian** — *weekly.* `api_call` to HN's RSS, `agent` (`historian`) renders the week's tech news as a chapter from Tacitus' *Annales*, a `loop` retries until fewer than two modern terms remain, `file_writer` writes `annales/MMXXVI-week-NN.md`. Ships as [`slow-roman-historian.json`](../../taktis/defaults/pipeline_templates/slow-roman-historian.json). Why orchestrated: the loop's quality predicate is what makes the persona arbitrage actually land.
- **Recursive Argument Engine** — *daily.* An `agent` proposes a thesis, `output_parser` splits it, two parallel agents argue for/against, an `aggregator` produces a synthesis, a `conditional` checks for genuine novelty and otherwise routes back through a `loop` for a sharper round. Ships as [`recursive-argument-engine.json`](../../taktis/defaults/pipeline_templates/recursive-argument-engine.json). Why orchestrated: the dialectic is the artifact.

And six worth building:

- **Liturgy of the Tides** — *daily.* `api_call` to NOAA tide data for a coast that matters to you, `agent` (`narratologist`) writes a brief tidal observance in the voice of a 19th-century lighthouse keeper, `file_writer` to `liturgy/YYYY-MM-DD.md`. Why orchestrated: the daily arrival is the point; one-off generation isn't liturgy.
- **Ghost Code Reviewer** — *weekly.* `fan_out` over commits older than five years still on `master`, `agent` (`reviewer-general`) reviews them as if newly written, `file_writer` writes `ghosts/YYYY-Wnn.md`. Why orchestrated: most code is never re-read; this is the only mechanism that re-reads it.
- **Cartographer of Lost Search Queries** — *weekly.* `api_call` to your search history export, `agent` (`search-query-analyst`) clusters queries that returned nothing useful, `agent` (`docs-writer-general`) drafts the page that would have answered each, `file_writer` to `unanswered/`. Why orchestrated: your unanswered questions are the seed of next year's notes.
- **Pipeline Generator as Tarot** — *weekly.* `agent` draws three random nodes from `NODE_TYPES`, `agent` interprets the spread as a creative directive, `pipeline_generator` materialises whatever spec falls out. Why orchestrated: surprise from a system that knows your library.
- **Greenhouse Dramaturgy** — *daily.* `api_call` to a soil-moisture / light-sensor endpoint, `agent` (`narrative-designer`) writes today's plot beat in the on-going drama between your plants, `file_writer` to `greenhouse/YYYY-MM-DD.md`. Why orchestrated: continuity. The drama needs to remember yesterday.
- **Dream Logs for Fictional Cities** — *daily.* `agent` (`geographer`) extends a stored map of a fictional city in `.taktis/cities/`, `agent` (`anthropologist`) writes one citizen's dream from last night, `file_writer` appends to `dreams/<city>.md`. Why orchestrated: the city is built by accretion — one daily entry at a time, for a year.

## How to use

Author a recipe in the visual designer at `/pipelines` — drop nodes, wire ports, save the template (it's stored as a row in the `pipeline_templates` table and shows up immediately). For a recipe you want to ship as a built-in, drop a `*.json` file into `taktis/defaults/pipeline_templates/`; on next startup `_seed_pipeline_templates()` inserts it (see [[Pipeline Factory]] → seeding behavior). To bind a template to a recurring run, open `/schedules`, pick the template + project, choose hourly / daily / weekly / monthly — the [[Cron Scheduler]] refuses to bind anything that contains a `human_gate` or interactive `agent`.

## Related

[[Cron Scheduler]] · [[Pipeline Factory]] · [[Node Types]] · [[Expert System]] · [[Architecture Overview]]
