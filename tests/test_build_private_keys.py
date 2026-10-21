"""No real keys or Windows vault writes; validate only the new setup contract."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "workstreams/build/private_key_setup.py"
SPEC = importlib.util.spec_from_file_location("basketball_private_setup", PATH)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class FakeStore:
    def __init__(self):
        self.values = {}
        self.calls = []

    def put(self, name, value):
        self.calls.append(("put", name))
        self.values[name] = value

    def get(self, name):
        self.calls.append(("get", name))
        return self.values.get(name)


class PrivateKeysTests(unittest.TestCase):
    def test_only_exact_new_basketball_provider_names_are_accepted(self):
        for provider in setup.PROVIDERS:
            self.assertEqual(setup.target(provider), setup.PREFIX + provider)
        for provider in ("football", "../openai", "OpenAI", "tavily/other", ""):
            with self.assertRaises(ValueError):
                setup.target(provider)

    def test_saved_result_never_contains_raw_key_or_its_hash(self):
        store = FakeStore()
        value = "FAKE-TEST-NOT-A-REAL-KEY"
        result = setup.save_and_verify(store, "tavily", value)
        self.assertEqual(result, {"provider": "tavily", "saved_and_verified": True})
        self.assertNotIn(value, str(result))
        self.assertEqual(store.calls, [("put", "tavily"), ("get", "tavily")])

    def test_bad_key_shape_rejects_before_store_access(self):
        store = FakeStore()
        for value in ("", "has space", "line\nbreak", "é", "x" * 2561, None):
            with self.assertRaises(ValueError):
                setup.save_and_verify(store, "tavily", value)
        self.assertFalse(store.calls)

    def test_noninteractive_mode_cannot_touch_vault(self):
        with patch.object(setup.sys, "argv", [str(PATH)]), patch.object(setup.sys.stdin, "isatty", return_value=False), \
                patch.object(setup, "WindowsBasketballStore") as store, patch("builtins.print"):
            self.assertEqual(setup.main(), 1)
            store.assert_not_called()

    def test_declined_storage_does_not_construct_store(self):
        with patch.object(setup.sys, "argv", [str(PATH)]), patch.object(setup.sys.stdin, "isatty", return_value=True), \
                patch.object(setup.sys.stdout, "isatty", return_value=True), patch("builtins.input", return_value=""), \
                patch.object(setup, "WindowsBasketballStore") as store, patch("builtins.print"):
            self.assertEqual(setup.main(), 0)
            store.assert_not_called()
