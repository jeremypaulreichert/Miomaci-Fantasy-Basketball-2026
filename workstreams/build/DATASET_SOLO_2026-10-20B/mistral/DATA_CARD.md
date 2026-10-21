# Miomaci AI Fantasy Basketball Solo Roster: mistral

Status: **draft, not published.** License, landing page, author credit and DOI are owner decisions.

## Motivation
A dated, reproducible record of how 2026 frontier AI models research and decide under identical rules, built so future models can be compared on the same decision points.

## Composition
- `decisions.jsonl`: 13 scheduled turns (one row each, including never-reached turns).
- `calls.jsonl`: 19 model and tool calls.
- `forecasts.jsonl` / `consistency.jsonl`: 0 / 0 sealed calls, if present.
- Identifiers: `mbd1-` decision IDs allocated before the first request (`DECISION_IDS` file).
- `timing.json`: first counted tipoff, final pick time and the scoring-window start.

## Timing and scoring window
- First counted tipoff: 2026-10-20 19:00 UTC. Final pick settled: 2026-10-20 20:44 UTC. Scoring window starts: 2026-10-20 20:53 UTC.
- First pick settled: 2026-10-20 14:00 UTC.
- Only regular-season games tipping off after the final settled pick count, for every roster.
- Turns never reached show zero calls and zero usage; token and charge totals are null only when a sent call did not report them.
- Some picks settled after the first tipoff; those picks could research games already played, although those games are not scored.

## Run history
The draft ran in segments: a stopped run's settled picks were carried into a continuation run that began at the next pick, under the same draft order, rules and player packet. Each row names its segment and run; calls a stopped run made on the pick it stopped at are kept and marked `counted_in_decision: false`. An earlier attempt that was restarted from pick 1 is a separate, labelled comparison, not part of these records.
- Segment 1: `research-incident-quota-counted-solo-mistral-20261020a`, picks 1-8, ended PAUSED (INCOMPLETE_RESPONSE).
- Segment 2: `research-incident-quota-counted-solo-mistral-20261020b`, picks 9-13, ended COMPLETE.

## Collection
Each AI manager received the same rules and player packet, could make up to two web searches and two page reads per pick, and returned a JSON action. All requests, responses, usage and timings were journaled append-only with a hash chain before export.

## What is withheld
Search queries, page text, full URLs and free-text rationales are withheld unless the release states otherwise (content clearance pending). Provider reasoning/thinking blocks are never exported. Source domains are included.

## Uses and limits
- Descriptive: one draft, one season; seat order and luck are not controlled away.
- Model outputs are not ground truth. Estimated charges use maximum list rates, not invoices.
- Token and charge totals are null when any call did not report them; `*_known` and `*_missing_calls` give the reported subtotal and the gap. Unreported reasoning tokens are unknown, not zero.
- Rows for calls still in flight (UNKNOWN_DISPATCH / SAVED_NOT_APPLIED) carry identity and timing only.
- Open turns show the baseline pick with no chosen value.
- A held-out evaluation split, if released, must carry the evaluation canary string; training-friendly releases should not.

## Maintenance
Versioned releases; corrections create a new version linked to the old one. Earlier versions are kept.
