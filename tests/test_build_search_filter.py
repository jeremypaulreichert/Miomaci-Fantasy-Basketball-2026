"""A search result with a disallowed URL is dropped and recorded, not fatal (October 19, 2026)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fantasy_basketball.providers.research_service import normalize_tool

SEARCH = {"action": "search", "query": "q", "url": None, "player_id": None, "rationale": None}


def row(url, text="evidence"):
    return {"url": url, "title": "t", "content": text, "score": 0.5}


class SearchFilterTests(unittest.TestCase):
    def test_http_row_is_dropped_and_recorded(self):
        raw = {"results": [row("https://a.example.com/x"), row("http://www.espn.com/nba"), row("https://b.example.com/y")],
               "usage": {"credits": 1}}
        out = normalize_tool(SEARCH, raw, retrieved_ms=5)
        self.assertEqual([i["url"] for i in out["evidence"]["items"]], ["https://a.example.com/x", "https://b.example.com/y"])
        self.assertEqual(out["receipt"]["skipped_results"], [{"url": "http://www.espn.com/nba", "reason": "URL_POLICY"}])

    def test_all_rows_disallowed_still_rejects(self):
        with self.assertRaises(ValueError):
            normalize_tool(SEARCH, {"results": [row("http://x.example.com/")]}, retrieved_ms=5)

    def test_clean_search_receipt_unchanged_and_reads_stay_strict(self):
        out = normalize_tool(SEARCH, {"results": [row("https://a.example.com/x")]}, retrieved_ms=5)
        self.assertNotIn("skipped_results", out["receipt"])
        read = {"action": "read", "query": None, "url": "http://x.example.com/", "player_id": None, "rationale": None}
        with self.assertRaises(ValueError):
            normalize_tool(read, {"results": [{"url": "http://x.example.com/", "raw_content": "t"}]}, retrieved_ms=5)


if __name__ == "__main__":
    unittest.main()
