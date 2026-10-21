# Methods and limitations

This is an adaptive, organizer-supervised deployment study, not a fixed-resource benchmark. The shared draft spans 14 segments. Time windows, output caps and correction instructions changed after observed failures; some failed turns received fresh opportunities. Settled choices were retained. The prior independent audit matched the retained choices to AI answers and found no organizer replacement selections. That audit is retained privately and has not been rerun by this companion verifier.

The draft finished at 20:53:21.015 UTC on October 20, 2026. Seven retained picks settled after the first scheduled 19:00 UTC game started. The common scoring rule excludes games starting at or before the final shared pick, including that first game. This companion reports the corrected shared boundary; it does not copy the historical solo timing files or implement a season scorer.

Each roster has 13 players. Weekly best-ball scoring selects the best legal eight-player lineup retrospectively: two guards, two forwards, one center and three utility players. The weights and position slots are in `rules.json`. This is roster selection with hindsight lineup optimization, not AI in-season team management. Historical data have missing values; this release contains no season outcomes or complete player-stat packet.

The independent solo rosters are a different availability condition, not controlled repeats of the shared draft. Roster overlap is descriptive. Seat, availability, timing, resource changes and season luck prevent causal model rankings. Native API models may also change beyond the organizer's control.

Activity fields in the pick files describe the retained decision episode from the stitched export. They omit earlier superseded episodes. In particular, `decision_wall_ms` is successful-episode wall time, not total completion time, and the call/search/read fields cannot establish total experiment usage or reliability. No invoice, balance, account-level spending or aggregate cost claim is included. No search calls does not prove exclusive reliance on supplied data.

Some local seals were created after dispatch or a response. Local hashes alone are not public preregistration. This companion makes no claim that every amendment or decision ID was externally committed before execution. Raw journals and failed attempts remain private; readers cannot independently verify their event chains from these companion files.

For a second experiment, freeze cumulative per-turn limits, explicit failure and fallback rules, and a durable continuation policy before starting. Qualify representative late-board cases; record every attempt and operator wait; use rotated-seat repeats to separate some draft-position effects. Preserve failed runs rather than silently replacing them with final successes.
