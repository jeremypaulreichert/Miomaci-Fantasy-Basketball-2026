"""Pre-registered metrics: hand-checkable cases."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workstreams/build/analysis"))

import prereg_metrics as m

COMPANIES = ["Alibaba (Qwen)", "Anthropic", "DeepSeek", "Google", "Meta", "Mistral AI", "Moonshot AI", "OpenAI", "Z.ai", "xAI"]


def forecast(provider, order, prob=0.5):
    return dict(provider=provider, failure=None,
                top_players=[dict(player_id=p, projected_points=1000 - i, probability_top_25=prob) for i, p in enumerate(order)],
                league_winner=[dict(company=c, probability=0.1) for c in COMPANIES])


class MetricTests(unittest.TestCase):
    def setUp(self):
        self.actuals = {f"p{i}": float(100 - i) for i in range(40)}   # p0 best ... p39 worst
        self.top = [f"p{i}" for i in range(25)]

    def test_perfect_and_reversed_forecasts(self):
        rows = m.forecast_metrics([forecast("a", self.top, 1.0), forecast("b", list(reversed(self.top)), 1.0)], self.actuals)
        a, b = rows
        self.assertEqual((a["top25_overlap"], a["spearman_vs_actual"], a["brier_top25"]), (1.0, 1.0, 0.0))
        self.assertEqual((b["top25_overlap"], b["spearman_vs_actual"]), (1.0, -1.0))

    def test_brier_and_missing_actuals_are_handled_honestly(self):
        order = [f"p{i}" for i in range(15, 40)]                       # 10 in the true top 25, 15 not
        rows = m.forecast_metrics([forecast("c", order, 0.5)], self.actuals)
        self.assertAlmostEqual(rows[0]["brier_top25"], 0.25)
        self.assertEqual(rows[0]["top25_overlap"], 10 / 25)
        partial = {k: v for k, v in self.actuals.items() if k != "p0"}
        r = m.forecast_metrics([forecast("d", self.top)], partial)[0]
        self.assertEqual(r["missing_actuals"], 1)                    # excluded and counted, never scored as zero
        bad = m.forecast_metrics([dict(provider="e", failure="FORECAST_SHAPE")], self.actuals)[0]
        self.assertEqual(bad["status"], "NO_VALID_FORECAST")

    def test_winner_scores(self):
        w = m.winner_metrics([forecast("a", self.top)], "OpenAI")[0]
        self.assertAlmostEqual(w["p_actual_winner"], 0.1)
        self.assertAlmostEqual(w["log_score"], round(__import__("math").log(0.1), 4))
        self.assertAlmostEqual(w["brier_multiclass"], round(0.81 + 9 * 0.01, 4))

    def test_zero_probability_winner_is_scored_at_the_floor_not_dropped(self):
        f = forecast("z", self.top)
        f["league_winner"] = [dict(company=c, probability=(0.0 if c == "OpenAI" else 1 / 9)) for c in COMPANIES]
        w = m.winner_metrics([f], "OpenAI")[0]
        self.assertTrue(w["log_score_floored"])
        self.assertAlmostEqual(w["log_score"], round(__import__("math").log(m.PROB_FLOOR), 4))

    def test_ties_at_the_cutoff_share_the_top_group_and_modal_ties_are_stable(self):
        actuals = dict(self.actuals, p25=self.actuals["p24"])           # p25 ties the 25th total
        r = m.forecast_metrics([forecast("t", [f"p{i}" for i in range(1, 26)], 1.0)], actuals)[0]
        self.assertEqual(r["top25_overlap"], 1.0)
        rows = [dict(provider="a", player_id="y"), dict(provider="a", player_id="x")]
        self.assertEqual(m.consistency_metrics(rows)[0]["modal_pick"], "x")

    def test_winner_rows_keep_failures_and_reject_unknown_company(self):
        rows = m.winner_metrics([forecast("a", self.top), dict(provider="b", failure="FORECAST_SHAPE")], "OpenAI")
        self.assertEqual([r.get("status") for r in rows], [None, "NO_VALID_FORECAST"])
        with self.assertRaises(ValueError):
            m.winner_metrics([forecast("a", self.top)], "Open AI")

    def test_consistency(self):
        rows = [dict(provider="a", player_id="x"), dict(provider="a", player_id="x"), dict(provider="a", player_id="x"),
                dict(provider="b", player_id="x"), dict(provider="b", player_id="y"), dict(provider="b", failure="F")]
        a, b = m.consistency_metrics(rows)
        self.assertTrue(a["all_agree"])
        self.assertEqual((b["distinct"], b["valid"], b["all_agree"]), (2, 2, False))
        self.assertAlmostEqual(b["modal_share"], round(1 / 3, 4))

    def test_decisions_and_solo_overlap(self):
        shared = [dict(status="SETTLED", manager="a", player_id="p1", baseline_player_id="p0", baseline_minus_chosen_fppg=2.0,
                       decision_wall_ms=60000, searches=2, reads=1),
                  dict(status="SETTLED", manager="a", player_id="p0", baseline_player_id="p0", baseline_minus_chosen_fppg=0.0,
                       decision_wall_ms=0, searches=0, reads=0),
                  dict(status="NOT_REACHED", manager="a")]
        d = m.decision_metrics(shared, self.actuals)[0]
        self.assertEqual((d["settled"], d["same_as_baseline"], d["mean_prior_fppg_baseline_minus_chosen"]), (2, 1, 1.0))
        self.assertEqual(d["mean_realized_total_baseline_minus_chosen"], 0.5)
        solo = [dict(status="SETTLED", manager="a", player_id="p1"), dict(status="SETTLED", manager="a", player_id="p9")]
        o = m.solo_overlap(shared, [solo])[0]
        self.assertEqual((o["common"], o["jaccard"]), (1, round(1 / 3, 4)))

    def test_compute_end_to_end_without_actuals(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "decisions.jsonl").write_text(json.dumps(dict(status="SETTLED", manager="a", player_id="p1",
                                                               baseline_player_id="p0", baseline_minus_chosen_fppg=1.5)) + "\n")
            result = m.compute(d)
            self.assertFalse(result["actuals_supplied"])
            self.assertNotIn("forecasts", result)


if __name__ == "__main__":
    unittest.main()
