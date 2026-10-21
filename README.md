# Miomaci Fantasy Basketball 2026

A reduced research archive of an AI fantasy basketball experiment, with selected source code, datasets and independent reviews.

Ten configured AI managers completed a shared 130-pick snake draft and ten independent 13-player rosters. This was an adaptive, organizer-supervised deployment study. Limits and correction instructions changed during execution; it is not a fixed-resource ranking of companies or intelligence.

## Start here

- [Small companion and offline verifier](companion/README.md): selections and roster overlap without raw working records.
- [Corrected shared dataset](workstreams/build/DATASET_DRAFT_2026-10-20B/DATA_CARD.md) and [corrected solo datasets](workstreams/build/DATASET_SOLO_2026-10-20B/).
- [Lessons and version two](reviews/CLAUDE_LESSONS_AND_V2_2026-10-20_REV3.md).
- [Independent lessons](reviews/CODEX_LESSONS_V2_INDEPENDENT_2026-10-20.md) and [post-draft review](reviews/CODEX_POSTDRAFT_REVIEW_2026-10-20.md). These are historical reports, not new rechecks of this release.
- [First errata](workstreams/build/FRONTIER_LAUNCH_2026-10-19/ERRATA_2026-10-20.md) and [corrected dataset errata](workstreams/build/FRONTIER_LAUNCH_2026-10-19/ERRATA_2026-10-20B.md).

## What readers can verify

Run `python companion/verify.py` using Python 3.11 or newer for offline companion hashes, 260 selections, roster counts, overlap arithmetic and the common cutoff. No network is needed. The project source and tests are an archival subset: many tests depend on omitted build tools, fixtures or journals. Their inclusion is not a claim that this reduced repository runs the whole draft or its full test suite.

The original private archive remains unchanged. [OMITTED_FILES.md](OMITTED_FILES.md) records every excluded tag path and hash, including all journals. [PRIVACY_DERIVATIVES.json](PRIVACY_DERIVATIVES.json) records any retained files changed for privacy. Historical manifests and public seals remain exact; they may reference omitted originals or original versions of derivatives. Such references do not certify the replacement bytes. Full raw event-chain verification requires withheld evidence.

Three `.ots` proofs are retained from the tag unchanged; the corrected-dataset proof is an explicit post-tag supplement copied unchanged. They apply to the exact original seal-file bytes, not every file in this new repository. Local creation dates and hashes alone are not independent preregistration; no new calendar or blockchain verification was performed for this release.

The final shared pick was 2026-10-20 20:53:21.015 UTC, after the first scheduled game started. Seven picks settled after that tipoff. Games starting at or before the common cutoff are excluded for every roster. [The scoring-window manifest](workstreams/build/SCORING_WINDOW_MANIFEST_2026-10-20.json) governs that boundary. No season winner or invoice-confirmed total cost is asserted here.

## Privacy and rights

No journals, raw captured pages, copied browser profiles, screenshots, billing history, approval conversations or live-launch packages are included. Dataset model labels, timings, token counts, estimated experiment charges and failure categories are research observations, not private account balances. Brief public reasons, where present, are model outputs rather than private chain-of-thought.

The [MIT license](LICENSE) covers code. [CC BY 4.0](LICENSE-DATA.md) covers project-authored records and data only to the extent Jeremy Reichert holds those rights; [license scope](LICENSE_SCOPE.md) preserves third-party exclusions. Privacy checks cannot establish third-party licensing rights.

Dataset metadata files (CITATION.cff, croissant.json, dataset.jsonld) preserve the sealed export’s unfilled license, landing-page and DOI placeholders; the root LICENSE files govern this repository.

