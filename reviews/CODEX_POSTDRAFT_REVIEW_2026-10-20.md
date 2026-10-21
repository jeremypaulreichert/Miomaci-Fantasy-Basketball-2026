# Codex post-draft review — October 20, 2026

## Verdict

**The saved shared draft is complete and internally traceable to AI answers. The release is not yet ready for a scientifically reliable export.** I found two concrete P2 export defects, as well as material protocol and disclosure limitations. I found no evidence of an organizer substituting a player choice, a continuation changing a settled choice, or a missing settled pick.

The defensible description is **an adaptive, organizer-assisted deployment study of ten configured AI managers**, followed by descriptive season outcomes. It is not a clean, fixed-resource comparison of ten companies' underlying intelligence. The interventions are part of the result, not a reason to discard the result.

No confirmed P1 data-substitution or journal-integrity defect was found in the examined records. The P2 findings below should be resolved or explicitly disclosed before a public research release. None requires rerunning the paid draft or changing its choices.

## Independence, scope and method

I wrote `reviews/CODEX_LESSONS_V2_INDEPENDENT_2026-10-20.md` before opening the new October 20 evidence and have left it unchanged. That report discloses the unavoidable limitation: this assignment itself already revealed completion, continuations and broad amendment categories. It is a prospective reconstruction based on my recheck, not a genuinely outcome-blind preregistration.

I read the requested launch amendments and rules, the October 19–20 review reports, public seals, allocations, settled exports, shared and solo packages, and relevant current implementation against the archived originals. Shared status documents are stale and were not treated as evidence of the completed run.

Verification was independent of the application reducers:

- Used Python standard-library JSON, hashing and SQLite reads. Opened the 26 journals corresponding to the shared segments and solo final/partial exports only with `mode=ro&immutable=1`, after checking for sidecars. Checked table sets, bindings, event sequence/configuration/previous hashes and event digests. Their file hashes matched the exports and were unchanged across the checks.
- Compared every continuation's entire prior-results list to the preceding export, including request/response hashes and settlement times; checked predecessor run/configuration/journal/count bindings. Recomputed all 14 shared configuration hashes using the documented default-field exclusions.
- Joined all 130 shared settled choices, and all 130 final solo choices through their original segments, to native response JSON. Compared both player ID and public rationale, and matched the shared choices' request-body hashes. Checked each choice against its saved decision-time legal list and ownership, and independently checked final roster uniqueness and positional-slot feasibility.
- Recomputed every `PUBLIC_SEAL_*.json` manifest fingerprint and compared listed files with current bytes; distinguished later source edits from changes to sealed packages. Recomputed all 2,210 allocated IDs, including unused/superseded allocations. Deterministic-ID correctness does not prove when allocation occurred.
- Compared archived source variants with current code. All 38 distinct runtime source path/hash pairs referenced by the examined continuation/solo packages were recoverable somewhere in the project's current or archived code. This is a byte-availability check, not execution of those historical versions.
- Evaluated the current exporter's boundary guard against the real continuation boundaries. Executed only the isolated, pure `timing()` function extracted through Python's AST, on in-memory data from the final exports, to reproduce the solo-window problem. Did not import or execute application entry points.

No test suite, replay command, provider call, network request, permit command, credential access or key-store access was made. Prior reviewers' test counts are their evidence, not tests rerun here. I did not verify invoices, external model identity beyond the saved native fields, OpenTimestamps proofs, public publication, or future actuals scoring. Local hash chains establish internal consistency, not independent proof against someone rewriting an entire local record.

Only the two report files authorized by Jeremy were written. No implementation, journal, existing seal, status document or other evidence was repaired.

## Ranked findings

### P2-1 — The current stitched exporter rejects this actual continuation chain

Source: [counted_export.py](<../workstreams/build/decision_dataset/counted_export.py#L716>).

`stitch()` requires boundaries to be strictly increasing:

```python
if boundaries != sorted(boundaries) or len(set(boundaries)) != len(boundaries):
    raise ValueError("continuations must move forward")
```

The real H→J→K→…→V chain has boundaries:

```text
53, 56, 57, 75, 96, 96, 96, 116, 116, 117, 123, 123, 123
```

N, O, Q, T and U made paid attempts but settled no new pick. Their equal boundaries are legitimate and necessary to retain their failed calls. The current guard rejects them. Omitting them is not a sound workaround: it breaks the predecessor chain and loses attempts, latency and exposure.

**Fix before release:** accept a verified nondecreasing boundary sequence, preserving zero-progress segments and their calls. Keep all source/configuration/prefix checks. Represent empty pick intervals explicitly rather than displaying ranges such as 97–96. Verify a full 14-segment export against all 382 shared call records. The earlier export review accepted a simpler chain; its passing tests do not establish support for this chain.

### P2-2 — Solo timing exports do not implement the promised shared scoring window

Sources: [counted_export.py](<../workstreams/build/decision_dataset/counted_export.py#L750>), [counted_entry.py](<../workstreams/build/counted_entry.py#L105>), and `FRONTIER_LAUNCH_2026-10-19/SCORING_WINDOW_RULE_2026-10-19.md`.

The rule promises solo scoring no earlier than the later of the shared and solo final picks. However, `timing()` receives only the current run and calculates `max(own_final_pick, first_tipoff)`. There is no shared-final-pick argument. `counted_entry.roster()` similarly reports the current roster's own last pick as its cutoff.

Reproduction using the actual exports and the isolated current `timing()` function:

| Export | Returned window start UTC | Required common start UTC |
|---|---|---|
| Shared V | Oct 20 20:53:21.015 | Oct 20 20:53:21.015 |
| Solo Gemini A | Oct 20 19:00:00.000 | Oct 20 20:53:21.015 |
| Solo Mistral B | Oct 20 20:44:10.778 | Oct 20 20:53:21.015 |

The current settled-result files do not themselves contain a scored season, so this is a release/scoring-input defect, not evidence that an incorrect score has already been published. For tonight's scheduled games the erroneous solo cutoffs still happen to exclude the 19:00 game under a strict `>` rule, but the metadata contradicts the protocol and is unsafe to reuse.

**Fix before actuals:** one experiment-level scoring-window manifest bound to the shared and all solo final journals, carrying `1792529601015` milliseconds and the rule hash. Make every roster export and actuals release consume it. Test a game between solo completion and shared completion, a game exactly at the boundary, and a game after it. Do not rely on a prose instruction overriding machine-readable timing.

### P2-3 — Per-run correction bounds were reset across continuations; final success hides the actual resource policy unless attempts are joined

Sources: `src/fantasy_basketball/draft/research.py:305` onward; `workstreams/build/prepare_frontier_launch.py:242`; packages J–V; all predecessor settled exports. Relevant disclosure: `COUNTED_AMENDMENT_O_2026-10-20.md` and `COUNTED_AMENDMENT_V_2026-10-20.md`.

Every examined continuation's first request has an empty `research_history` and fresh allowance of nine model calls, two searches and two reads. Settled picks survive; the failed pick's accumulated correction/research history and counters do not. Its opportunity clock and run budget also restart. The fixed experiment hard limit remains unchanged; these resets do not bypass that calendar boundary. These are new attempts at the same experimental decision, not merely a process resuming unchanged state.

In the complete shared chain, pick 117 took **eight model requests** across P/Q/R; pick 124 took **eleven** across S/T/U/V. The latter exceeds the advertised nine-call bound if “per pick” means the logical draft turn rather than one segment's episode. Both ultimately settled AI-produced answers; that does not restore a common fixed opportunity budget.

**Assessment:** preserving completed choices through a continuation was the correct operational move. Repeated owner-approved attempts were understandable for completing the project. I disagree with treating their cumulative resource use as equivalent to one bounded correction policy. There was no frozen cross-segment attempt ceiling or fallback stopping rule in these packages.

**For this release:** preserve every failed episode and show both first-episode and eventual completion, cumulative requests/tokens/cost exposure, and end-to-end time including intervention waits. Do not calculate reliability or speed from only the successful segment. Do not retrospectively replace failed episodes with forfeits or call the final outcome unassisted.

Each shared package has its own $1,250 cap. Summing 14 such caps gives $17,500 of nominal per-package capacity, **not actual spend and not proof of aggregate owner authorization**. I did not inspect permits. Publish an aggregate ledger distinguishing native usage estimates, retained unknown exposure and invoice-confirmed spend, including F and solo arms separately. A single $1,250 label must not imply an experiment-wide bound.

### P2-4 — Seal claims need a precise, qualified chronology

Sources: `PUBLIC_SEAL_2026-10-20Q.json`, `PUBLIC_SEAL_2026-10-20S.json`, [COUNTED_ADDENDA](<../workstreams/build/FRONTIER_LAUNCH_2026-10-19/COUNTED_ADDENDA_2026-10-20.md:3>), [make_public_seal.py](<../workstreams/build/make_public_seal.py#L35>), and the two solo B packages.

- Q START is 18:14:10.687 UTC; its first RESULT is 18:17:00.282; its seal says 18:19. Q's seal was after an observed response, not just after dispatch.
- S START is 18:43:29.276; first RESULT is 18:45:58.668; its seal says 18:46. This seal likewise postdates a response. Deterministic IDs do not repair precommitment.
- N's narrative amendment was written after N's seal and covered in O, as disclosed. Keep that fact attached to N.
- The addenda say seals R and S were after the 3 p.m. tipoff. That is false: R is **18:25 UTC / 2:25 p.m. ET**, S **18:46 / 2:46 p.m.** Tipoff was **19:00 UTC**. T, U, V and the final-rosters seal are after tipoff.
- I found no supplied pre-start public-seal manifest binding the two **solo B** continuation packages/configurations. The initial solo proposal binds A configuration hashes, and the final-rosters seal binds finished outputs; neither proves pre-start commitment to B. Report these as locally journal-bound continuations without demonstrated prior public sealing unless additional evidence is produced.
- All examined public-seal records have `published: false`. Some have `.ots` companions, which I did not validate offline. A local timestamp and a correct SHA-256 manifest are not by themselves independently witnessed public preregistration.

**Correction:** publish an append-only timing/coverage table with separate fields for configuration commitment, dispatch, first response, local seal creation and external timestamp/publication evidence. Mark post-response seals as such. Do not silently edit old sealed files to make their claims true.

### P2-5 — The completed run cannot be represented as a fixed, equal-treatment frontier benchmark

Sources: output-cap amendments L/M/N/O/P/R/S/U/V and their actual package profiles; H/J recovery changes; `SOLO_ROSTERS_AMENDMENT_2026-10-20.md`.

Limits changed in response to observed failures. Some later seats received different token ceilings, a longer window and additional fresh episodes; earlier settled picks were retained under older rules. New feedback explicitly urged immediate/minimal deliberation or corrected legality/shape. These interventions change the AI's inputs, computation opportunity and probability of reaching a valid pick even when scoring and legal-player rules stay identical.

It is legitimate to study a provider-tuned deployed system, provided that tuning and organizer effort are named components of the treatment. It is not legitimate to attribute season standings solely to company/model quality, or to call the conditions identical because every seat received thirteen eventual players.

**Correction before publication:** freeze the descriptive claim now, publish the full intervention matrix, and keep reliability under the first episode distinct from eventual roster quality. Any comparative causal claim needs new, prospectively designed replications; it cannot be repaired by statistical adjustment to this one draft.

### P3 — Small but definite record and reproducibility corrections

1. There are **14 executed shared segments** H, J, K, L, M, N, O, P, Q, R, S, T, U, V. There are 13 predecessor exports; I was prepared/sealed but not executed. F is a separate aborted attempt, not one of the 14 and not part of the final prefix.
2. `FINAL_PICK_AND_SCORING_WINDOW_2026-10-20.md:19` says “roughly picks 117–130” settled after tipoff. The exact range is **124–130**. Pick 123 settled at **18:58:54.816 UTC**. Failed attempts at 124 crossed tipoff before V's final successful attempt.
3. That document's approximate Mistral solo finish “4:41 p.m.” should be **4:44:10.778 p.m. ET** / 20:44:10.778 UTC, from its final result. This remains before the shared final pick.
4. The generic seal tool's “before the first counted tipoff” text is unconditional and wrong for later seals. Correct future wording; preserve the originals with a linked erratum.
5. Current shared STATUS/DECISIONS and the older post-draft export instructions do not describe the complete chain. HQ should update navigation/summary documents after this review, without rewriting sealed history. Most implementation files are untracked in Git; archived source hashes help, but a reviewed immutable source/release snapshot is still needed.
6. Several seal entries name mutable `prepare_frontier_launch.py`; K–P and SOLO no longer match that path's current bytes. R's addenda hash also differs after later additions. Those mismatches are not proof of pick alteration: their package/choice bindings check out. Preserve the exact old sealed bytes under immutable archive paths, and expose a verifier that resolves historical bytes rather than presenting a current-path mismatch as a successful whole-seal verification.

## Verified timeline and why each stop led to the next segment

Times below are UTC; subtract four hours for Eastern. Ranges end at the last journal event, not at an inferred wall-clock operator action. Counts are cumulative shared settled picks. References for every row are its package, same-suffix predecessor export (V uses `FINAL_ROSTERS_SHARED_2026-10-20.json`), native journal and corresponding `COUNTED_*AMENDMENT*` document.

### Before H

- F START: **Oct 19 18:24:06.264**; last journal event **19:08:04.399**, with 16 picks retained. Mistral's next pick exhausted its 32,768 cap. The restart amendment/report describes the incident somewhat later; the journal times above are the observed event times.
- The owner chose a full restart from pick 1, keeping F separate. The replacement introduced decide-now feedback sharing the format-correction slot, medium effort for xAI/Qwen, 20-minute call limits and 30-minute pick windows. Unsafe search-result rows were dropped and recorded instead of rejecting an entire search. H began after the replacement seal.
- I was prepared after H stopped, but J superseded it after a broader stop-path audit. No I execution was found in this counted chain.

| Segment | START → last event | Prior → settled | Observed stop / outcome | Chosen next action |
|---|---|---:|---|---|
| H, Oct 19 | 20:22:47 → 22:55:01 | 0 → 53 | Pick 54 Z.ai: valid player, 525-character rationale; `PICK_SHAPE` | Preserve 53; add bounded shape recovery and later stop-path extensions; prepare I then run J |
| J, Oct 20 | 10:14:03 → 10:50:16 | 53 → 56 | Pick 57 Mistral: HTTP 504 (~900.7 s); retry names already-owned Kevin Porter Jr.; correction worker timeout (~347.3 s), `RESPONSE_LATE` | K restarts 57 with a 45-minute pick window |
| K | 11:01:09 → 11:13:39 | 56 → 57 | Mistral settles 57; DeepSeek at 58 exhausts 16,384 twice, `INCOMPLETE_RESPONSE` | L raises DeepSeek/Gemini/Z.ai to 32,768, response bounds to 1 MiB |
| L | 11:23:39 → 12:38:13 | 57 → 75 | Pick 76 xAI: illegal answer, correction HTTP 520; `MODEL_TRANSPORT_FAILED` | M restarts 76; also raises Mistral to 49,152 after earlier near-cap behavior |
| M | 12:44:58 → 15:19:35 | 75 → 96 | Pick 97 Mistral HTTP 520 (~725.6 s). Earlier Mistral calls also show ~900.7 s HTTP 504s | N lowers Mistral to 24,576, modeled on solo performance |
| N | 15:23:45 → 15:40:51 | 96 → 96 | Pick 97: two 24,576 cap exhaustions (~438.7/586.1 s) | O raises Mistral to 36,864 |
| O | 15:56:50 → 16:12:58 | 96 → 96 | Pick 97: HTTP 429, then automatic retry HTTP 504 (~900.8 s) | P sets Mistral to 30,720 |
| P | 16:19:41 → 18:08:49 | 96 → 116 | Pick 117: length exhaustion; decide-now chooses already-drafted Naz Reid; correction exhausts cap | Q repeats with unchanged 30,720 cap |
| Q | 18:14:10 → 18:20:15 | 116 → 116 | Pick 117: two further cap exhaustions | R raises Mistral to 49,152; Q seal is late |
| R | 18:26:07 → 18:39:19 | 116 → 117 | Mistral settles 117 on third request; DeepSeek 118 exhausts 32,768 twice | S raises DeepSeek to 49,152 |
| S | 18:43:29 → 19:11:41 | 117 → 123 | Pick 124 Mistral: already-owned Ty Jerome; cap exhaustion; already-owned LaMelo Ball; `ILLEGAL_PICK` | T starts a fresh attempt with unchanged caps; S seal is late |
| T | 19:36:57 → 19:47:03 | 123 → 123 | Pick 124: cap exhaustion; already-owned Andrew Nembhard; nonlegal player ID; `ILLEGAL_PICK` | U lowers Mistral to 30,720 |
| U | 19:55:28 → 20:04:24 | 123 → 123 | Pick 124: two further cap exhaustions | V raises Mistral to 65,536 |
| V | 20:10:15 → 20:53:21 | 123 → 130 | Mistral's first two answers at 124 are again already owned; third is legal. Remaining seats finish | Complete; OpenAI's Damian Lillard settles last at 20:53:21.015 |

HTTP 504 near 15 minutes supports an upstream time-limit hypothesis. It does not prove that changing output caps alone prevents it. HTTP 429 during concurrent solo/shared execution is consistent with shared rate pressure, but these records do not prove the concurrency caused it. I disagree with converting either hypothesis into a guaranteed engineering rule.

### Solo timeline

All ten A runs START around **13:57 UTC on Oct 20**, while M's shared run is active. Eight finish their thirteen picks directly. Z.ai A stops with nine settled (`PICK_SHAPE`); Mistral A with eight (`INCOMPLETE_RESPONSE`).

- Z.ai B START **14:16:58.814**, finishes **14:23:07.620**, preserving the nine A picks and adding four. Same 32,768 cap.
- Mistral B START **20:24:47.677**, finishes **20:44:10.778**, preserving the eight A picks and adding five, at 30,720 rather than A's 24,576.
- The other eight final times range from Meta at **14:02:20.564** through xAI at **14:40:43.013**. All ten final solo rosters precede **20:53:21.015**.
- Each final solo roster has thirteen distinct eligible IDs and a feasible eight-slot lineup. Overlap between different solo managers is intentional.

## Amendment-by-amendment assessment

| Change | Operational judgment | Effect on AI inputs/opportunity or judgment; evenness |
|---|---|---|
| Pre-H full restart, decide-now, effort/time changes | Reasonable after a failed counted attempt, with F retained separately. It sacrificed a fixed first attempt in favor of a new adaptive run. | xAI/Qwen effort changed specifically; all future H calls receive longer limits. F cannot be silently merged with H or treated as an independent randomized replicate. |
| Search-row filtering; failed-read feedback | Sensible service robustness. Keeping valid rows is preferable to stopping because one URL is unusable. | Changes which evidence is presented and whether another model turn occurs. Same implemented rule for future calls; dropped/failed evidence must remain recorded. “Unchanged research” would be inaccurate. |
| H→I/J shape/stop-path expansion | A 525-character rationale is a poor reason to lose a draft; a frozen formatter policy should have existed beforehand. Keeping a failed call and letting the AI reformat is preferable to organizer trimming. | New correction feedback changes prompts. Second illegal-pick correction, refusal/hold re-asks and late-response decide-now broaden the accepted completion process. Legal/scoring tests stay fixed, but early settled picks did not all have the same recovery opportunity. |
| K 30→45-minute window | Justified as an emergency completion measure after a 15-minute provider error consumed the opportunity. | Applies to future turns, not retrospectively to earlier ones; resets failed pick 57. Therefore not uniform treatment over the whole draft. Longer windows cannot fix every provider timeout. |
| L DeepSeek/Gemini/Z.ai 16,384→32,768 and larger response envelopes | DeepSeek exhaustion supports trying more room. Raising the other two preemptively is plausible deployment tuning, not a controlled causal test. | Provider-specific compute and response limits change. Same legal/scoring rules. Requires actual per-segment profile hashes, not stable profile IDs as proof of identical settings. |
| M Mistral 32,768→49,152 | Plausible preventive intervention, not proof that this cap is optimal. | Mistral-specific setting; increases potential deliberation/cost and may increase server-timeout risk. |
| N/O/P Mistral 24,576→36,864→30,720 | Understandable troubleshooting, weak experimental control. Solo success did not establish a transferable shared late-board cap. | Fresh histories, deadlines and correction pools accompany each change. No isolated comparison can identify whether cap, server load, board difficulty or chance caused success. |
| Q unchanged retry; R 49,152 | Keeping settings unchanged in Q is a repeat attempt, not a neutral resume. R follows observed failures and speed. | More cumulative opportunities for Mistral; Q commitment is post-response. Outcome-dependent intervention must be part of the account. |
| S DeepSeek 49,152 | Reasonable response to repeated 32K exhaustion. | DeepSeek-specific change; later DeepSeek opportunities differ from early ones. Late seal prevents a pre-response-seal claim for S. |
| T unchanged retry; U 30,720; V 65,536 | Legitimate owner preference to let Mistral choose rather than auto-pick. I disagree that repeated tuning until a valid answer is methodologically equivalent to one prebounded turn. | Eleven requests at logical pick 124, with fresh context and caps. Legality judgment is stable; chance of eventually passing and total cost/time are not. |
| Solo addition during shared M | Valuable exploratory contrast: unconstrained player access versus competition. Adding it after seeing shared behavior is an amendment, not a preregistered confirmatory arm. | Different start times, own-board histories, Mistral cap and later B recoveries. Concurrent API load can couple arms. Same scoring window is promised, but needs the export fix. |
| Forecast attempt rule | Honest about being written after attempts were observed. Attempt-one failures retained as primary is the appropriate disclosure. | Re-ask recovery is secondary, with different caps. Do not pool successful forecasts from both attempts as a single uniform pre-registered sample. |
| Games after final shared pick | This was already bound in the initial pre-F seal; it is not a post-hoc decision to drop a disappointing game. Appropriate to honor it. | Applies the same eligible-game window to every completed roster. Does not erase the later managers' opportunity to research in-progress games. |

The package comparison found no shared drift in the player pool, fixed row order, scoring rules, counted task, seat models or seat order from H through V. Changes outside source bindings/run IDs/prior prefixes were the documented opportunity window and output/response caps. The counted task hash is `b886d5b957a7d28d2d6177eb5b167428c9aa2e1bf8a62771cf40397312a91d45` throughout. Recovery implementation changes still alter the actual multi-call prompts even when that task hash remains constant.

## Integrity and timing results

### Seals versus starts

The following are **declared local seal times**, to minute precision, compared with journal START. They are not independent external timestamps.

| Run | Local seal UTC | Journal START UTC | Assessment |
|---|---|---|---|
| F | Oct 19 18:19 | Oct 19 18:24:06 | Earlier |
| H | Oct 19 20:20 | Oct 19 20:22:47 | Earlier |
| I | Oct 19 23:14 | No execution in chain | Unused |
| J | Oct 19 23:43 | Oct 20 10:14:03 | Earlier |
| K | Oct 20 11:00 | 11:01:09 | Earlier |
| L | 11:23 | 11:23:39 | Same minute; sub-minute ordering not proved by seal field |
| M | 12:43 | 12:44:58 | Earlier |
| N | 15:23 | 15:23:45 | Same-minute ambiguity; amendment narrative separately late |
| O | 15:56 | 15:56:50 | Same-minute ambiguity |
| P | 16:19 | 16:19:41 | Same-minute ambiguity |
| Q | 18:19 | 18:14:10 | After START and first RESULT |
| R | 18:25 | 18:26:07 | Earlier, also before tipoff |
| S | 18:46 | 18:43:29 | After START and first RESULT, but before tipoff |
| T | 19:36 | 19:36:57 | Same-minute ambiguity; after tipoff |
| U | 19:54 | 19:55:28 | Earlier than its START, after tipoff |
| V | 20:09 | 20:10:15 | Earlier than its START, after tipoff |
| Solo A cohort | 13:56 | ~13:57 | Proposal/config-hash commitment earlier |
| Final rosters | 21:00 | After completion | Output preservation, not pre-draft commitment |

All seal manifest fingerprints recompute. Shared package/ID/export entries examined match their listed bytes. Whole-manifest current-path checks are not all clean because the builder and addenda were edited later, as detailed in P3. Treat that distinction explicitly rather than claiming every current path still matches every historical seal.

### Exact prior picks and native authorship

Every predecessor export is an exact prefix of V's 130 results. Every next package contains exactly that predecessor's results and correct source binding. Equal-count continuations preserve failed episodes without altering those results. Solo B prefixes exactly preserve their A partial exports.

For every final choice there is matching native text with the same player ID and rationale. All shared picked IDs were present in the supplied legal list and absent from current ownership; the request context's pick index agrees. All 130 shared IDs are distinct; the snake schedule gives thirteen to each of ten managers. Independent bipartite slot matching confirms each final shared and solo roster can fill G/G/F/F/C/U/U/U. These checks support “the AI made the recorded picks.” They do not imply the organizer was absent: the organizer changed settings, provided correction messages and authorized new episodes.

The final Mistral V attempt illustrates why the distinction matters: the system rejected its first two owned-player answers and accepted the third legal AI answer. It did not choose that third player's ID itself. Archived directory names mentioning “autopick” are not evidence that an automatic pick occurred; the native joins are the relevant evidence.

### Complete request denominators

The 14 shared journals contain **281 model requests, 69 searches and 32 reads: 382 calls**. This includes failed/superseded episodes, not F or solo. A 200 response is not necessarily a valid action: cap exhaustion and illegal choices also returned HTTP 200.

| Seat | Model requests across shared segments | Final shared picks |
|---|---:|---:|
| Anthropic | 25 | 13 |
| Gemini | 45 | 13 |
| DeepSeek | 18 | 13 |
| Mistral | 43 | 13 |
| xAI | 24 | 13 |
| Meta | 20 | 13 |
| Z.ai | 18 | 13 |
| Moonshot | 14 | 13 |
| Qwen | 16 | 13 |
| OpenAI | 58 | 13 |

Model requests include research actions, so this table is **not a failure ranking**. Separate search/read choices, invalid actions, provider errors, output exhaustion and correction attempts before drawing a conclusion. Mistral has seven non-response transport outcomes across these shared requests; xAI one. J includes a worker timeout with no validated native usage. Do not equate zero estimated charge on such an outcome with proof of no charge.

### Scoring-window correctness

V's final pick is OpenAI, ID `espn:6606` (Damian Lillard), **20:53:21.015 UTC / 4:53:21.015 p.m. ET**, timestamp **1792529601015**. Its native answer and settlement match the final export. Every final solo roster finished before that instant, so all twenty rosters should use exactly this common boundary.

The saved schedule `evidence/data/raw/approved-2026-10-18/nba_schedule_2027.csv` records:

| Game ID | Scheduled start UTC | Rule consequence |
|---|---|---|
| 401909088, Boston–Detroit | Oct 20 19:00 | Excluded |
| 401909089, Philadelphia–New York | Oct 20 23:00 | Eligible |
| 401909090, Oklahoma City–San Antonio | Oct 21 01:30 | Eligible |

This verifies the intended exclusion against the repository's schedule, not actual tipoff timestamps fetched independently. The scorer must resolve actual starts and postponements under the frozen event policy, preserving provenance. No current scored actuals release was certified by this review. The shared timing function calculates the correct cutoff; the solo export path needs P2-2 fixed.

Picks **124–130**, and only those settled picks, occurred after the first scheduled tipoff. Excluding Boston–Detroit is fair to all rosters under the frozen calendar, but cannot remove information acquired from that game while later turns were being researched.

## Scientific interpretation and honest disclosure

### Supported

- A documented example of ten specified API models independently producing research actions, rationales and a full competitive roster under human-supervised operational recovery.
- Descriptive comparisons of selected players, research behavior, rationale–data consistency, forecast calibration where valid, cost/latency and reliability, **with all attempted episodes retained**.
- Season outcomes of these actual frozen rosters under common scoring and calendar rules.
- Exploratory shared-versus-solo contrasts. The solo arm removes contested availability, but also changes timing, decision context and some limits; it does not isolate one causal variable by itself.
- A valuable historical record of what building and operating such an experiment required, including failure modes and organizer work.

### Not supported

- “The best AI company,” a statistically established intelligence ranking, or a causal effect of internet research, cap size, draft position or model reasoning effort.
- A clean thirteen-observation-per-model independent sample: turns share boards and roster constraints, and season scores share players/games and injury shocks. Ten teams in one draw are not ten independent replications of the same treatment.
- Uniform opportunity limits throughout the run, fully autonomous recovery, complete pre-response public commitment, or success before the NBA season began.
- Private thought-process access. Returned rationales and any provider-emitted thinking are observable outputs, not verified faithful causal accounts of internal cognition. Publish public rationales and action/evidence trails; do not relabel them as a complete mind trace.
- “No effect” from settings changes merely because player-pool/scoring hashes did not change.

Suggested public methods language:

> The shared draft completed 130 AI-generated picks across 14 execution segments on October 19–20, 2026. Settled picks were preserved exactly. After observed failures, the organizer changed recovery policies, opportunity windows and provider-specific output limits, and restarted failed turns with fresh local histories and allowances. Every retained choice matches a native AI response; no organizer-selected replacement was used. Failed and superseded attempts remain part of the dataset. Some local seals were created after requests or responses; public timestamp verification is reported separately. The final seven picks settled after the first scheduled game began. All rosters exclude games starting before the final shared pick at 20:53:21.015 UTC. Results describe this adaptive deployed system and this draw, rather than a controlled ranking of companies.

Use that only alongside the detailed amendment table, exact profiles, source provenance and complete call accounting. It should not substitute for those records.

## Phase 1 recommendations against the observed result

| Prospective recommendation | Result of this review |
|---|---|
| Freeze an incident/fallback state machine across the whole logical pick | Strongly confirmed. Fresh continuations saved the roster, but also reset treatment bounds; pick 124 needed eleven model requests. |
| Rehearse exact live prompts on middle/late constrained boards | Confirmed. Full offline plumbing tests did not predict provider cutoff behavior, long deliberation or repeated owned-player choices late in a shared draft. |
| Calibrate per-seat token/time tails and freeze escalation ladders | Confirmed, with a qualification: more tokens are not always better. Mistral's changes in both directions and server speed variation defeat a simple monotonic “raise cap” solution. |
| Preserve all failures and seal before requests | Confirmed. Journals and exact prefixes preserved unusually strong evidence, while Q/S and solo B show why commitment must be enforced, not asserted by an allocation tool. |
| Clear legal IDs, named columns and decision-state hashes | Partly confirmed. These survived and made legality auditable. They were not sufficient for Mistral to use the legal list reliably. Don't blame a proven remaining column bug without evidence. |
| Repeated/rotated draws and separate arms | Still needed. The new solo arm adds information, but does not turn the one shared draw into replicated evidence. F's restarted opening is a disclosed diagnostic, not a randomized repeat draw. |
| Global cost/timeout policy, not only per-run reservations | Confirmed. Thirteen continuations make aggregate exposure and full incident time essential. |
| Prefer complete, honest descriptive release over overclaiming | Confirmed. The preserved record is scientifically useful despite interventions. |

One overly pessimistic reading of Phase 1 should be rejected: an ordinary incident did **not** require discarding every earlier pick. The exact-prefix continuation mechanism worked. The failed component was a frozen, globally bounded recovery policy and complete final export support, not all of the plumbing.

## Ranked version 2 work

1. **One durable logical decision identity across process/run changes.** Preserve cumulative calls, corrections, research history, unknown exposure and time. Freeze an escalation ladder and a final disposition before observing failures. For an AI-only competition, a failed slot can remain empty or be forfeited under the declared rule. If using an AI-precommitted ranking as a continuity arm, call it a separate policy; never invent an organizer pick.
2. **A small representative live qualification, then an immutable protocol.** Exact final prompts and board size, each seat at opening/middle/near-complete roster states; combine it with offline full-draft fault injection and restart tests. Include repeated length exhaustion, 15-minute upstream errors, HTTP 429/520, invalid IDs, long rationales and zero-progress continuations. Review changes as a coherent frozen release, not a succession of “all tests pass” assurances.
3. **An end-to-end release rehearsal, not just draft rehearsal.** Require a full 130-pick multi-segment export including no-progress runs, all failed calls, native joins, stable decision IDs, profile hashes and one shared scoring-window manifest for every arm. Fix P2-1 and P2-2 offline now.
4. **Separate experimental objectives.** Predeclare whether resource limits represent equal dollar budgets, equal wall time or provider-tuned practical deployment. These are different questions. Freeze settings within an arm; label any emergency deviation prospectively and retain intention-to-treat and eventual-completion summaries.
5. **Control information time and service interference.** Finish before a fixed information cutoff or use a fixed-time research corpus. If live-web research is the treatment, document as-of times and concurrency limits. Avoid concurrent arms competing for the same provider quota unless this is deliberately part of the deployment test.
6. **Replicate and rotate seats.** Multiple seeded draws with rotated positions; common-board independent picks where useful. Add simple offline baselines and research/no-research comparisons only when they answer declared questions. Budget for repeats rather than multiplying extras without controls.
7. **One experiment-level cost ledger and emergency ceiling.** Charge all failed/restarted episodes to the same logical decision and arm. Preserve uncertainty instead of replacing it with zero. Preapprove escalation/top-up rules; do not let a new run ID implicitly replenish the experimental budget.
8. **Make commitments and publication verifiable.** Seconds/milliseconds in seals, refusal to call a seal “pre-start” after START/RESULT, distinct local/external timestamp states, archive actual sealed source bytes, and a versioned release with corrected citations/data cards. Keep observer metadata out of manager prompts when it is not part of the task.

## Corrections to make now, without changing the draft

1. Repair/export-test zero-progress segment stitching and the solo/common cutoff. Do this offline; no new API calls or choice changes are needed.
2. Publish an append-only erratum for the after-tipoff range (124–130), R/S tipoff timing, Mistral solo finish, late Q/S seals and unverified solo B precommitment. Preserve existing sealed files.
3. Build a complete 14-segment release and per-pick intervention ledger. Include F as an excluded earlier attempt, I as unused, solo A/B as a distinct exploratory arm, and both forecast attempts under their declared primary/recovery labels.
4. Before any standings release, bind the common scoring cutoff and rule hash to actual game inclusion, then reconcile independently. A correct draft receipt alone does not certify the scorer.
5. Update the public claim and current project navigation to distinguish “complete AI-generated rosters” from “uniform preregistered conditions.” Preserve all choices and original journals.

The project has a strong evidence-preservation foundation. Its next scientific improvement is disciplined treatment definition and reliable analysis/release plumbing, not another expensive draft just to conceal the interventions this one already documents.

---

## Follow-up: actual stitched export and Claude's lessons document

This addendum follows Jeremy's message supplying the completed export and `D:/Downloads/basketball-lessons-and-v2-2026-10-20.md`. I also read the repository copy, `reviews/CLAUDE_LESSONS_AND_V2_2026-10-20.md`. The narrative and suggested future actions in those documents were treated as claims to evaluate, not as authorization to implement them. Earlier findings above remain the historical review; statuses below supersede them where explicitly stated.

### P2-1 closed for the actual exported chain

The current exporter permits equal consecutive boundaries. The actual `workstreams/build/DATASET_DRAFT_2026-10-20` export contains:

- 130 unique, settled decision rows, matching the final saved player IDs, turn indices, request/response hashes and settlement times exactly;
- 382 unique call rows across all 14 segments: 281 model requests, 69 searches and 32 reads;
- all 28 superseded model calls, with no missing or additional journal INTENT keys;
- matching native model request/response hashes and matching dispatch/result times, elapsed times, outcomes and HTTP statuses for the exported calls;
- per-decision model/search/read counts that reconcile to their assigned, counted call rows;
- the correct shared cutoff, 1792529601015, and seven picks settled after the first scheduled tipoff.

I independently rehashed the 14 original journals against the export manifest, opened them only read-only/immutable after checking sidecars, joined all calls and verified unchanged hashes afterward. Every file listed in the shared manifest and all ten solo manifests matches its listed SHA-256. The ten solo choice exports match their respective final saved choices, and their shared-versus-solo Jaccard figures recompute.

The 20/10/60 stored files covered by forecast A, forecast B and consistency seals respectively match their seal hashes, with no extra JSON files in the corresponding stores; all three manifest fingerprints recompute. This verifies local store preservation, not external publication or OpenTimestamps verification.

The current metric source matches the code hash in the original pre-draft public seal. Independent arithmetic reproduces the reported decision averages, baseline gaps, research counts and roster overlaps. The consistency records support eight providers with three valid identical Jokić answers, with Qwen/xAI each having one failed answer. These are meaningful positive checks. I did not rerun an application command, test suite or metric CLI, or write a replacement dataset.

### P2-2 remains open in all ten solo exports

Every `workstreams/build/DATASET_SOLO_2026-10-20/<provider>/timing.json` still has the wrong common-window field. Nine report 1792522800000 (19:00 UTC); Mistral reports 1792529050778 (20:44:10.778 UTC). All should report the shared final boundary, **1792529601015 (20:53:21.015 UTC)**, under the adopted common-window rule.

The shared export is correct. No game scoring was supplied here, and the three opening-night scheduled games happen to fall outside the discrepancy intervals. Nevertheless, the solo release metadata is not ready to govern scoring. Fix the common-cutoff input and version/regenerate the affected releases; preserve the existing draft versions. Do not simply hand-edit hashed timing files.

### P2 — Successful-episode speed must not be presented as full draft cost in time

Claude's 749.6 seconds / 12.5 minutes for Mistral reproduces the sealed `mean_decision_seconds` metric. It averages only the episodes that eventually supplied settled picks. It omits the earlier failed episodes at those same logical turns. This is the declared formula, not an arithmetic bug.

Adding elapsed time for **all** shared call records attributable to Mistral's thirteen turns, including its 21 superseded calls, gives **1,309.8 seconds / 21.8 minutes per settled pick**. This is a different, useful measure: active call time, still excluding operator waits and some local overhead. The corresponding figures are 407.4 seconds for xAI's successful episodes versus 439.3 seconds of all-call time per pick; Meta has no superseded calls and remains approximately 72 seconds.

Keep the sealed metric unchanged and label it “successful-episode wall time.” Add explicitly post-hoc companion measures for all-attempt active time, cumulative opportunity episodes and owner-wait time. “Mistral took 12.5 minutes per pick” without that qualification understates the completion burden. Superseded calls belong in reliability and overall expense summaries even though they do not determine the retained choice.

### P2 — Baseline comparisons need coverage denominators

The reported baseline gaps reproduce, but the number of usable selected-player comparisons differs:

| Manager | Non-null prior-value gaps out of 13 |
|---|---:|
| OpenAI | 8 |
| Gemini | 10 |
| Anthropic | 12 |
| Each other manager | 13 |

Gemini's missing-value choices include Cameron Boozer and AJ Dybantsa, but those rows are **excluded** from its mean gap, not treated as low/zero prior production. Consequently, “Gemini's gap partly reflects its rookie bets” is not an established explanation of the calculated 4.42. Missing rows change the coverage and composition of the mean; they do not directly contribute a numeric penalty.

Publish usable-pair counts beside the means and identify missing-value choices. Do not use proximity to the naive prior-stat baseline as a measure of intelligence or actual season performance. The sealed formula should remain preserved; add a coverage table rather than silently changing it after seeing results.

### Lessons write-up: agreements and corrections

I agree with deeper live rehearsal, a frozen failure/fallback policy, one durable continuation flow, provider concurrency controls and rotated repeats. The document also needs these qualifications before being relied on:

1. **Costs:** the five listed deltas total **$156.58**, not $165. This is arithmetic on Claude's reported billing readings, not independently verified invoices. The remaining-provider total is provisional. Account-level before/after readings also need matching dates, projects, credits and other-usage scope before being called experiment-attributable spend. The supplied text alternates between Sunday readings and an October 19 baseline. Do not silently add a whole-month Gemini total to a draft-period delta.
2. **Count corrections:** H/J/K–V are 14 shared segments, plus separate aborted F makes 15 executed runs across those two categories. “13 runs” is wrong. The journals show **eleven model requests at pick 124**, not the lessons table's thirteenth-request claim.
3. **Rehearsal wording:** there was a full offline 130-pick rehearsal. The specific deficiency was short/native qualification and insufficient representative late-board provider behavior, not absence of any full-length rehearsal.
4. **Research wording:** DeepSeek/Mistral/Moonshot/Qwen made no calls through the common search/read service in the shared draft. That does not prove they relied exclusively on the packet: pretrained knowledge and unsupported assumptions remain possible. The raw research-count observation is supported; the exclusive-information-source claim is too strong.
5. **Preferences:** solo rosters show substantial convergence: 18 unique players across all ten rosters, nine distinct roster sets, pairwise solo Jaccard 0.625–1.0. However, same-manager solo/shared overlap is only 0.04–0.1304. These facts do not identify unchanged preferences or prove draft position is the dominant cause. Availability, accumulated roster needs, settings, timing and chance differ. Describe convergence and overlap; reserve causal explanations for matched-availability tests.
6. **Unchanged inputs:** the base packet and scoring rules remained stable. Correction prompts, available time, output limits and prior failed-turn history did not. The blanket claim that “what the AIs saw stayed the same” still needs correction.
7. **Streaming:** it is a candidate mitigation to qualify, not a demonstrated cure for these 504s. The saved records do not establish which proxy/server timer caused them or whether streaming resets it. Preserve the timeout hypothesis as a hypothesis until a bounded representative test and provider-specific contract support it.
8. **Retries:** do not promote “504 and 429 are safe to resend” to a universal rule. An upstream timeout does not prove that generation did not occur or was unbilled. A new generation can also change the experimental answer. Distinguish explicit pre-dispatch rejection, saved/late result, uncertain dispatch and a separately bounded new opportunity; retain exposure and attempt history. This review authorizes no retry.
9. **Fallback:** a baseline-selected player occupying an “empty-scoring” slot still removes that player from competitors' boards. It is not scientifically neutral merely because its score is zero. Jeremy's existing no-organizer-pick rule remains applicable. Prefer an explicitly frozen actual forfeit/empty slot for the primary AI-only arm, or a separately declared continuity arm; no fallback was adopted here.
10. **Budget ratios:** observed bill/list-rate ratios can inform expected-spend forecasts. They should not replace conservative request/exposure limits or be assumed stable across providers, cache conditions and later runs.

### Release disclosure still needs correction

`DATASET_DRAFT_2026-10-20/DATA_CARD.md` still claims all decision IDs were allocated before the first request. Q is the known counterexample. It also renders empty settled intervals as 97–96, 117–116 and 124–123. The former is a substantive precommitment overstatement; the latter is a presentation defect. Carry the Q/S seal caveats and intervention matrix into the actual release's data card, not only a separate historical report.

**Follow-up verdict:** the shared stitching repair is demonstrated by the actual export, and the raw evidence remains valuable. Accept the shared choice/call reconciliation for this dataset. Keep release/scoring acceptance pending the solo cutoff correction and qualified metadata/analysis language. No paid rerun is indicated.
