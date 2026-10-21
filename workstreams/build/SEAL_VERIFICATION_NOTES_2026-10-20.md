# Seal verification notes — October 20, 2026

Jeremy authorized these eight known current-path mismatches as documented exceptions after independent verification. No existing seal, source snapshot, amendment, package, journal, export or review was modified for this verification. No network, provider, permit or credential operation was performed.

All paths below are relative to the repository root. Each recovered version was read as bytes and its SHA-256 compared directly with the indicated seal entry. A current-path mismatch remains a mismatch; this note identifies the matching historical bytes instead of rewriting history.

## Seven builder-file entries

The current path in every row is `workstreams/build/prepare_frontier_launch.py`. Archived paths are relative to `workstreams/build/FRONTIER_LAUNCH_2026-10-19/original-source/`.

| Public seal | Archived file with the exact sealed bytes | Verified SHA-256 |
|---|---|---|
| `PUBLIC_SEAL_2026-10-20K.json` | `output-cap-L/prepare_frontier_launch.py` | `652575e84ffb002e88f3178d09f5fa9fd89abd86149278dc89928c2b16f750dc` |
| `PUBLIC_SEAL_2026-10-20L.json` | `amendment-M/prepare_frontier_launch.py` | `59100bbf884f2a738faf8c0d11255ac44c5eb860cc2f7da254f8b79b7bb582bd` |
| `PUBLIC_SEAL_2026-10-20M.json` | `amendment-N/prepare_frontier_launch.py` | `99397545bb20979634282c32b9f0f8a55d69cf88419f27f3e731f9fee97eb71c` |
| `PUBLIC_SEAL_2026-10-20N.json` | `autopick-O/prepare_frontier_launch.py` | `0e0c4f74044423f1d7185a39001e1c4b18dc98a9b707c521186bf386769ae050` |
| `PUBLIC_SEAL_2026-10-20O.json` | `solo-continuation/prepare_frontier_launch.pre-cap.py` | `6b58b0a6e84aabf1de156b5bdc953781fa763d79263fd1edfb21218f0ea92e2b` |
| `PUBLIC_SEAL_2026-10-20P.json` | `solo-continuation/prepare_frontier_launch.pre-cap.py` | `6b58b0a6e84aabf1de156b5bdc953781fa763d79263fd1edfb21218f0ea92e2b` |
| `PUBLIC_SEAL_SOLO_2026-10-20.json` | `solo-continuation/prepare_frontier_launch.py` | `419ff88bcf998e4314380622bd0954aff0c5d9192fc6ec1ae7bfd52ba451e452` |

The seal files are under `workstreams/build/`. There are six distinct archived builder versions across these seven entries; O and P reference the same bytes.

## Seal R addenda entry

- Seal: `workstreams/build/PUBLIC_SEAL_2026-10-20R.json`.
- Current path: `workstreams/build/FRONTIER_LAUNCH_2026-10-19/COUNTED_ADDENDA_2026-10-20.md`.
- Exact historical bytes: the first **597 bytes**, indexed `[0:597]`, of the current file. This is a byte prefix, not a character count or newline-normalized text extraction.
- Verified SHA-256 of that prefix: `d596c0b2d90a3b3d634850b4351e54b0903b6bbf42135ac98d63b224b8821acd`.
- Current file length at verification: 948 bytes. The remainder consists of later appended material; the full current file does not match the old entry.

## Commit and push checkpoint

Treat these as the owner's explicitly authorized historical-byte exceptions, not unexplained integrity failures. Before push, verify the bytes in Git itself, not only the working tree: commit this note, all referenced seal files, the six archived builder files, and the current addenda containing the recoverable prefix. Every other directly listed public-seal entry must be committed and match its listed SHA-256 without an exception. Report any additional mismatch; do not repair or broaden the exception list implicitly.

This note does not certify external timestamps, public publication, the absence of secrets in other files, or readiness to commit or push. Commit approval and the owner's explicit “push” instruction remain separate.
