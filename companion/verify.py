"""Offline companion verification; Python standard library only."""
import collections
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
def load(name):
    return json.loads((root / name).read_text(encoding="utf-8"))
manifest = load("MANIFEST.json")
actual = {p.name for p in root.iterdir() if p.is_file()}
assert actual == set(manifest) | {"MANIFEST.json"}, "Unexpected or missing file"
for name, expected in manifest.items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
a, b = load("shared_picks.json"), load("solo_picks.json")
assert len(a) == len(b) == 130
assert [x["pick_number"] for x in a] == list(range(1, 131))
assert len({x["player_id"] for x in a}) == 130
providers = {x["provider"] for x in load("cohort.json")}
assert len(providers) == 10
for rows in (a, b):
    assert collections.Counter(x["manager"] for x in rows) == {p: 13 for p in providers}
    for p in providers:
        assert len({x["player_id"] for x in rows if x["manager"] == p}) == 13
for s in load("summary.json"):
    aa = {x["player_id"] for x in a if x["manager"] == s["manager"]}
    bb = {x["player_id"] for x in b if x["manager"] == s["manager"]}
    assert s["shared_picks"] == s["solo_picks"] == 13
    assert s["shared_solo_common_players"] == len(aa & bb)
    assert s["shared_solo_jaccard"] == round(len(aa & bb) / len(aa | bb), 6)
timing = load("timing.json")
assert timing["final_pick_settled_ms"] == timing["scoring_window_start_ms"] == 1792529601015
assert not timing["finished_before_first_tipoff"]
print("PASS: file hashes, 260 selections, roster counts, overlap arithmetic and shared cutoff")
