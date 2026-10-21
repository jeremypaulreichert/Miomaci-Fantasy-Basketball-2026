# AI fantasy basketball: lessons learned and a version-2 protocol (October 20, 2026, revision 3)

Written by Claude (Build) after the counted draft finished. This is a plain-language record of what went wrong, why, what it cost, and what a stronger version would do. Revision 3 applies Codex's follow-up review; the corrections are listed in `FRONTIER_LAUNCH_2026-10-19/ERRATA_2026-10-20B.md`. Revisions 1 and 2 are kept unchanged.

## Outcome in one paragraph

All 130 shared-draft picks and all 130 solo-roster picks (10 AIs × 13) match the AIs' own native answers. No automatic or organizer-chosen pick was used. Getting there took two kinds of runs:

- run F, an aborted earlier attempt, kept separately;
- 14 executed shared segments: H, J, K, L, M, N, O, P, Q, R, S, T, U and V. Each continued from the last settled pick. N, O, Q, T and U settled no new pick. Package I was prepared and sealed but never run.

Along the way the organizer:

- wrote 11 dated mid-run amendments;
- changed pick windows, per-seat output caps and recovery prompts;
- restarted failed turns with fresh allowances.

**So this is an adaptive, organizer-supervised deployment of ten configured models, not a fixed-condition ranking of companies.**

Timing:

- The final pick settled at 4:53:21 p.m. ET on October 20, after the 3 p.m. first tipoff. Picks 124–130 settled after that tipoff.
- Under the pre-registered window rule, the 3 p.m. Boston–Detroit game is not scored. The 7 p.m. and 9:30 p.m. games are.
- All 20 rosters use the same scoring start, 20:53:21.015 UTC. The final rosters were sealed at 5:00 p.m. ET and stamped with OpenTimestamps before the first counted game.

## What stopped the draft, and why

| # | Stop | Likely cause | Fix used |
|---|---|---|---|
| 1 | Unreadable Reddit page (pilot) | Any failed page read paused the run | Failed reads/searches become feedback in counted runs |
| 2 | Non-secure (http) search result (pilot) | One bad row rejected the whole search | Unsafe rows dropped, search continues |
| 3 | Mistral used its whole answer cap thinking (run F, pick 17) | Caps sized from early-round tests | "Decide now" warning, medium effort where available, longer limits; restart from pick 1 |
| 4 | Z.ai's reason 525 characters, limit 500 (run H, pick 54) | Format slips stopped the run | One format correction per pick; continuation from the last settled pick |
| 5 | Mistral: server timeout + illegal pick + window ran out (run J, pick 57) | 30-minute window too short for an outage plus a retry plus a correction | 45-minute window |
| 6 | DeepSeek used its whole 16K cap twice (run K, pick 58) | Late-round picks drew far more thinking | DeepSeek, Z.ai and Gemini raised to 32K |
| 7 | xAI illegal pick, then a server "520" error (run L, pick 76) | Ambiguous server errors are never resent automatically (by design) | Restart; Mistral raised to 48K |
| 8 | Mistral "520" error after 12 minutes (run M, pick 97) | Long Mistral requests | Mistral set to 24K |
| 9 | Mistral used its whole 24K twice (run N) | Too little room | 36K |
| 10 | Mistral rate limit (429), then a server error near 15 minutes (run O) | Possibly solo/shared competition for its quota; not proven | 30K, solo paused |
| 11 | Mistral full cap, then an illegal pick (run P, pick 117) | Board tracking | Retry at 30K |
| 12 | Mistral full cap twice (run Q) | Thinking ran past 30K late in the draft | 48K |
| 13 | DeepSeek used its whole 32K twice (run R, pick 118) | Late-round thinking | DeepSeek raised to 48K |
| 14 | Mistral illegal picks: Ty Jerome, LaMelo Ball, Andrew Nembhard (runs S, T, U, pick 124) | Board tracking | 30K, then 64K; Mistral made a legal pick on its **11th** request at that pick |

Solo runs had two stops of their own:

- Z.ai: a format slip. Restarted from pick 10.
- Mistral: ran out of room at 24K. Restarted from pick 9 at 30K.

Neither restart was publicly sealed before it started.

## The big lessons

1. **The live rehearsal didn't look like the real draft.** There was a full offline 130-pick rehearsal of the plumbing. The live, real-provider practice, however, covered only early picks, where choices are easy. Thinking grew sharply in later rounds: DeepSeek went from about 8K tokens to more than 32K, and Mistral from about 18K to more than 49K. Caps and windows were sized from early-round evidence.
2. **There was no frozen policy for a pick that keeps failing.** "Stop rather than guess" protected the evidence, but each stop then needed a fresh owner decision. Every continuation also reset the failed pick's clock, research/correction allowance and history:
   - pick 117 took 8 model requests across three runs;
   - pick 124 took 11 across four runs, more than the 9 a single run allows.

   122 of 130 picks settled in their first episode. `pick_attempts.jsonl` records every episode.
3. **Provider limits we didn't measure.**
   - Several Mistral requests ended with HTTP 504 at about 15.01 minutes. That suggests an upstream time limit, but these records don't prove which timer it was.
   - Mistral's useful cap band looked narrow in this draft, roughly 30–45K. Too little room exhausted the cap, and more room ran long. Server speed, board difficulty and chance are not separated.
   - Two "520" errors (xAI and Mistral) were treated as unknown outcomes and stopped the run. That is safe but slow.
   - The HTTP 429 came while Mistral's solo and shared runs overlapped. That is consistent with shared quota pressure, but not proven.
4. **Board-tracking failures are a real observation.** Mistral named already-drafted players repeatedly late in the draft, including its own. xAI did it once earlier. Every request contained the full board and legal list. This is worth measuring prospectively; one draft doesn't establish a model trait.
5. **The amendments change what the results mean.** Each was dated and disclosed, but they were outcome-dependent.
   - **Stayed the same:** the base player packet, scoring rules, player pool, seat order and counted task.
   - **Changed:** correction and recovery prompts, available time, per-seat output caps, and failed-turn history.
   - **Seal timing:** two continuation seals (Q and S) were made after a first response, so they are not pre-response commitments.
6. **The human was the bottleneck at every restart.** Each restart took 7 pasted commands and about 10–15 minutes. Skipped steps caused late seals, and wrong-folder runs failed twice. This argues for a single "continue" command.
7. **Things that worked and should be kept:**
   - append-only hash-chained journals;
   - never re-sending an unknown outcome;
   - exact-prefix continuations, with every boundary verified;
   - public seals plus OpenTimestamps;
   - "missing is never zero" accounting;
   - the forecast attempt rule;
   - exports that refuse anything that doesn't match its seal.

## What it cost

Dashboard readings from the evening of October 20 were compared with readings taken before the draft (October 19, about 2:05 p.m. ET). These are account-level dashboard numbers, not invoices, and may include other usage on those accounts.

| Provider | Before | After | Delta |
|---|---|---|---|
| OpenAI | $19.63 October spend | $112.07 | $92.44 |
| Anthropic | $20.63 month spend | $65.04 | $44.41 |
| xAI | $25.90 credits | $14.76 | $11.14 |
| Moonshot | $4.14 total consumption | $10.75 | $6.60 |
| DeepSeek | $1.42 total cost | $3.41 | $1.99 |
| **Five providers** | | | **$156.58** |
| Gemini | — | October total $8.35 (lags hours) | not final |
| Mistral, Meta, Z.ai, Qwen | — | not checked | unknown |

- **No confirmed experiment total exists yet.** The rest is likely small, but it has not been checked.
- Personal account-limit details are withheld from this privacy derivative. Experiment cost estimates and their scope caveats are retained.
- **Journal estimates:** our journals estimated $344 at maximum list rates for runs H–V plus solo. The checked dashboards ran well below that. Such ratios can inform forecasts, but they should not replace conservative request limits.

## Version 2: a stronger protocol, ranked by impact

1. **A frozen failure/fallback policy across the whole logical pick.** Before the draft, fix the per-pick request, time and cost ceilings. These ceilings carry across restarts instead of resetting. Also fix the escalation steps and the final disposition. For an AI-only primary arm, prefer a forfeited empty slot. A baseline autopick still removes a player from other boards, so it isn't neutral. Any AI-precommitted ranking would be a separate, declared continuity arm. Never use an organizer pick.
2. **Live qualification on realistic boards, then freeze.** Use the exact counted prompts on opening, middle and late boards for every seat, with repeats where results vary. Set caps and windows from measured tails, and freeze them, so no cap amendments happen during the counted run.
3. **An end-to-end release rehearsal.** Rehearse a multi-segment export including zero-progress runs, failed calls and one shared scoring-window manifest for every arm, before the real draft.
4. **A provider-error table, decided in advance.** For each status, distinguish:
   - explicit pre-dispatch rejection;
   - a saved or late result;
   - uncertain dispatch;
   - a new, separately bounded opportunity.

   Keep every attempt's exposure. Don't assume any status is free to resend.
5. **Test streaming as a candidate transport.** It may avoid the ~15-minute cutoff, but that is a hypothesis to qualify in rehearsal, not an established fix. Apply it to every seat that supports it.
6. **Equal, explicit board-state feedback.** Illegal-pick corrections should be identical for every seat, with no basketball coaching.
7. **One-command continuation** with one approval prompt, plus seals that refuse to call themselves "pre-start" after a START or RESULT event.
8. **No concurrency per provider** unless that is deliberately part of the test.
9. **Repeat draws with rotated seats.** One draft is one sample. Add simple baselines and research/no-research arms only for declared questions.
10. **One experiment-level cost ledger.** Charge failed and restarted episodes to the same logical pick. Preserve unknowns. Snapshot balances automatically before and after.

## First numbers from the dataset (pre-season, no game results yet)

The release dataset is `workstreams/build/DATASET_DRAFT_2026-10-20B`, with all 130 decisions and 382 calls from all 14 segments. Pre-season metrics from the sealed scoring code are in `METRICS_PRESEASON_2026-10-20B.json`. The post-hoc companion measures are in `POSTHOC_MEASURES_2026-10-20.json`.

**Research calls per pick (searches / page reads):**

| AI | Searches | Reads |
|---|---|---|
| OpenAI | 1.92 | 1.54 |
| Gemini | 1.69 | 0.69 |
| Anthropic | 0.62 | 0.15 |
| xAI | 0.62 | 0.08 |
| Meta | 0.31 | 0 |
| Z.ai | 0.15 | 0 |
| DeepSeek, Mistral, Moonshot, Qwen | 0 | 0 |

The last four used no search or read calls. Their picks came from the packet plus whatever the models already knew.

**Time per settled pick:**

| AI | Successful episode (sealed metric) | All attempts, active call time (post hoc) |
|---|---:|---:|
| Mistral | 749.6 s (12.5 min) | 1,309.8 s (21.8 min) |
| xAI | 407.4 s | 439.3 s |
| Meta | 72.2 s | 71.6 s |

The all-attempt figure still excludes owner waiting between runs.

**Distance from the naive baseline pick.** This is the average prior-season fantasy points per game given up (baseline minus chosen). It measures closeness to "take the best last-season producer", not intelligence or season results. Averages cover only picks where both values exist.

| AI | Mean gap | Usable picks (of 13) |
|---|---:|---:|
| DeepSeek | 2.12 | 13 |
| Anthropic | 2.74 | 12 |
| OpenAI | 2.84 | 8 |
| Gemini | 4.42 | 10 |
| xAI | 4.45 | 13 |

Moonshot's gap is 4.15, with all 13 picks usable.

**Consistency test:** 8 of 10 AIs gave the same first pick (Jokić) all three times. Qwen and xAI each had one invalid answer.

**Solo versus shared.** The two arms differ in availability, roster needs, settings, timing and chance. Explaining the difference needs a matched-availability test.

- Solo rosters converge strongly: 18 distinct players across all ten, 9 distinct roster sets, and pairwise Jaccard 0.625–1.0.
- Each AI's solo and shared rosters overlap little: Jaccard 0.04–0.13.

## What the data can and can't support

**It can support:**

- descriptive comparisons of research behavior, rationales, cost, latency and reliability, with every attempt retained;
- each AI's observed failure modes in this run;
- shared-versus-solo overlap, described rather than explained;
- season outcomes of these frozen rosters under the common scoring window.

**It can't support:**

- causal claims that one company's AI is "better at fantasy basketball";
- uniform conditions across seats or across the draft;
- claims that streaming or any retry rule would have prevented the stops;
- any claim about the excluded 3 p.m. game.
