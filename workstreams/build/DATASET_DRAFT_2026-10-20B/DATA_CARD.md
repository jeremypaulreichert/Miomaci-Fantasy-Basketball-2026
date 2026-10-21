# Miomaci AI Fantasy Basketball Draft Decisions (2026-27 season)

Status: **draft, not published.** License, landing page, author credit and DOI are owner decisions.

## Motivation
A dated, reproducible record of how 2026 frontier AI models research and decide under identical rules, built so future models can be compared on the same decision points.

## Composition
- `decisions.jsonl`: 130 scheduled turns (one row each, including never-reached turns).
- `calls.jsonl`: 382 model and tool calls.
- `forecasts.jsonl` / `consistency.jsonl`: 10 / 30 sealed calls, if present.
- Identifiers: `mbd1-` decision IDs allocated before the first request (`DECISION_IDS` file).
- `timing.json`: first counted tipoff, final pick time and the scoring-window start.
- `forecasts.jsonl` is attempt 1 only (all seats; primary). `forecasts_recovery.jsonl` gives each seat's first valid forecast within the declared attempts (separately labelled); `forecasts_all_attempts.jsonl` keeps every attempt, failures included.

## Timing and scoring window
- First counted tipoff: 2026-10-20 19:00 UTC. Final pick settled: 2026-10-20 20:53 UTC. Scoring window starts: 2026-10-20 20:53 UTC.
- First pick settled: 2026-10-19 20:23 UTC.
- Only regular-season games tipping off after the final settled pick count, for every roster.
- Turns never reached show zero calls and zero usage; token and charge totals are null only when a sent call did not report them.
- Some picks settled after the first tipoff; those picks could research games already played, although those games are not scored.

## Run history
The draft ran in segments: a stopped run's settled picks were carried into a continuation run that began at the next pick, under the same draft order, rules and player packet. Each row names its segment and run; calls a stopped run made on the pick it stopped at are kept and marked `counted_in_decision: false`. An earlier attempt that was restarted from pick 1 is a separate, labelled comparison, not part of these records.
- Segment 1: `research-incident-quota-counted-frontier-20261019h`, picks 1-53, ended PAUSED (PICK_SHAPE).
- Segment 2: `research-incident-quota-counted-frontier-20261019j`, picks 54-56, ended PAUSED (RESPONSE_LATE).
- Segment 3: `research-incident-quota-counted-frontier-20261020k`, picks 57-57, ended PAUSED (INCOMPLETE_RESPONSE).
- Segment 4: `research-incident-quota-counted-frontier-20261020l`, picks 58-75, ended PAUSED (MODEL_TRANSPORT_FAILED).
- Segment 5: `research-incident-quota-counted-frontier-20261020m`, picks 76-96, ended PAUSED (MODEL_TRANSPORT_FAILED).
- Segment 6: `research-incident-quota-counted-frontier-20261020n`, picks None-end, ended PAUSED (INCOMPLETE_RESPONSE).
- Segment 7: `research-incident-quota-counted-frontier-20261020o`, picks None-end, ended PAUSED (MODEL_TRANSPORT_FAILED).
- Segment 8: `research-incident-quota-counted-frontier-20261020p`, picks 97-116, ended PAUSED (INCOMPLETE_RESPONSE).
- Segment 9: `research-incident-quota-counted-frontier-20261020q`, picks None-end, ended PAUSED (INCOMPLETE_RESPONSE).
- Segment 10: `research-incident-quota-counted-frontier-20261020r`, picks 117-117, ended PAUSED (INCOMPLETE_RESPONSE).
- Segment 11: `research-incident-quota-counted-frontier-20261020s`, picks 118-123, ended PAUSED (ILLEGAL_PICK).
- Segment 12: `research-incident-quota-counted-frontier-20261020t`, picks None-end, ended PAUSED (ILLEGAL_PICK).
- Segment 13: `research-incident-quota-counted-frontier-20261020u`, picks None-end, ended PAUSED (INCOMPLETE_RESPONSE).
- Segment 14: `research-incident-quota-counted-frontier-20261020v`, picks 124-130, ended COMPLETE.

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
