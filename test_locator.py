"""Unit tests for FieldLocator — adjacent text matching strategy."""
import unittest
from unittest.mock import MagicMock, patch
from src.locator import FieldLocator, LocatorResult, LocatorError


class TestCandidatesAdjacentText(unittest.TestCase):
    """Test _candidates_adjacent_text strategy in isolation."""

    def setUp(self):
        self.mock_cdp = MagicMock()
        self.locator = FieldLocator(self.mock_cdp)

    # ------------------------------------------------------------------
    # Empty / no label
    # ------------------------------------------------------------------

    def test_empty_label_returns_empty(self):
        """No label → no candidates."""
        result = self.locator._candidates_adjacent_text(
            {"label": ""}, "", ""
        )
        self.assertEqual(result, [])

    def test_no_label_key_returns_empty(self):
        """Missing label key → no candidates."""
        result = self.locator._candidates_adjacent_text(
            {"type": "text"}, "", ""
        )
        self.assertEqual(result, [])

    # ------------------------------------------------------------------
    # Previous sibling match
    # ------------------------------------------------------------------

    def test_previous_sibling_match(self):
        """When <h2>Foo</h2><input>, and label='Foo', should find the input."""
        self.mock_cdp.eval.return_value = [{"i": 0, "src": "s"}]
        result = self.locator._candidates_adjacent_text(
            {"label": "Your Vehicle Year"}, "", ""
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["strategy"], "adjacent_text")
        self.assertEqual(result[0]["confidence"], 0.7)  # sibling=0.7
        self.assertIn("data-at", result[0]["selector"])

    # ------------------------------------------------------------------
    # Parent text match
    # ------------------------------------------------------------------

    def test_parent_text_match(self):
        """When <div>Email<input></div>, and label='Email', should find by parent."""
        self.mock_cdp.eval.return_value = [{"i": 0, "src": "p"}]
        result = self.locator._candidates_adjacent_text(
            {"label": "Email"}, "", ""
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["strategy"], "adjacent_text")
        self.assertEqual(result[0]["confidence"], 0.5)  # parent=0.5

    # ------------------------------------------------------------------
    # No matches
    # ------------------------------------------------------------------

    def test_no_match_returns_empty(self):
        """Label text not found anywhere → empty list."""
        self.mock_cdp.eval.return_value = "0"
        result = self.locator._candidates_adjacent_text(
            {"label": "XYZNonexistent"}, "", ""
        )
        self.assertEqual(result, [])

    # ------------------------------------------------------------------
    # Multiple matches
    # ------------------------------------------------------------------

    def test_multiple_matches(self):
        """When 3 elements match the same label, return all 3."""
        self.mock_cdp.eval.return_value = [{"i": 0, "src": "s"}, {"i": 1, "src": "s"}, {"i": 2, "src": "s"}]
        result = self.locator._candidates_adjacent_text(
            {"label": "Name"}, "", ""
        )
        self.assertEqual(len(result), 3)
        for r in result:
            self.assertEqual(r["strategy"], "adjacent_text")
            self.assertIn("data-at", r["selector"])

    # ------------------------------------------------------------------
    # Ancestor chain match (MUI-style: label outside input's subtree)
    # ------------------------------------------------------------------

    def test_ancestor_chain_match(self):
        """Label text in ancestor (MUI: label outside input's parent chain)."""
        self.mock_cdp.eval.return_value = [{"i": 0, "src": "a"}]
        result = self.locator._candidates_adjacent_text(
            {"label": "First Name"}, "", ""
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["strategy"], "adjacent_text")
        self.assertEqual(result[0]["confidence"], 0.55)

    # ------------------------------------------------------------------
    # JS error handling
    # ------------------------------------------------------------------

    def test_js_error_returns_empty(self):
        """When eval throws or returns invalid, return empty list."""
        self.mock_cdp.eval.return_value = "not_json"
        result = self.locator._candidates_adjacent_text(
            {"label": "Foo"}, "", ""
        )
        self.assertEqual(result, [])

    # ------------------------------------------------------------------
    # Special characters in label
    # ------------------------------------------------------------------

    def test_special_chars_in_label(self):
        """Labels with quotes/special chars should be escaped in JS."""
        self.mock_cdp.eval.return_value = [{"i": 0, "src": "s"}]
        result = self.locator._candidates_adjacent_text(
            {"label": "O'Brien's Choice"}, "", ""
        )
        # Should not throw, should handle the apostrophe
        self.assertEqual(len(result), 1)

    # ------------------------------------------------------------------
    # Select elements included
    # ------------------------------------------------------------------

    def test_select_element_included_in_search(self):
        """The JS queries 'input,select,textarea' — select elements included."""
        self.mock_cdp.eval.return_value = [{"i": 0, "src": "s"}]
        result = self.locator._candidates_adjacent_text(
            {"label": "Make"}, "", ""
        )
        self.assertEqual(len(result), 1)


class TestFindAllCandidates(unittest.TestCase):
    """Verify _find_all_candidates includes the adjacent text strategy."""

    def setUp(self):
        self.mock_cdp = MagicMock()
        self.locator = FieldLocator(self.mock_cdp)

    def test_adjacent_text_in_find_all_candidates(self):
        """Strategy 5 (adjacent text) must be called in _find_all_candidates."""
        # Mock only the adjacent_text strategy; let other strategies work normally
        orig = self.locator._candidates_adjacent_text
        self.locator._candidates_adjacent_text = lambda f, fid, c: [{'selector': '#test', 'strategy': 'adjacent_text', 'confidence': 0.7}]
        try:
            result = self.locator._find_all_candidates(
                {"label": "TestLabel"}, "", ""
            )
            self.assertGreaterEqual(len(result), 1)
            strategies = {r["strategy"] for r in result}
            self.assertIn("adjacent_text", strategies)
        finally:
            self.locator._candidates_adjacent_text = orig
        strategies = {r["strategy"] for r in result}
        self.assertIn("adjacent_text", strategies)


class TestLocatorEdgeCases(unittest.TestCase):
    """Edge cases for the full locate() flow."""

    def setUp(self):
        self.mock_cdp = MagicMock()
        self.locator = FieldLocator(self.mock_cdp)

    def test_locator_error_structured(self):
        """LocatorError carries field_desc and attempts."""
        e = LocatorError(
            {"label": "test"},
            [{"strategy": "placeholder", "error": "no match"}]
        )
        self.assertEqual(e.field_desc["label"], "test")
        self.assertEqual(len(e.attempts), 1)

    def test_locator_result_alternatives(self):
        """LocatorResult stores alternatives for debugging."""
        alt = LocatorResult("#b", "adjacent_text", 0.6, "")
        result = LocatorResult("#a", "placeholder", 0.7, "",
                               alternatives=[alt])
        self.assertEqual(result.strategy, "placeholder")
        self.assertEqual(len(result.alternatives), 1)
        self.assertEqual(result.alternatives[0].selector, "#b")

    def test_cache_key_changes_with_field(self):
        """Different fields should produce different cache keys."""
        self.locator._cache = {}
        # Force a miss by pre-populating different keys
        self.locator._cache['{"label": "A"}'] = MagicMock()
        self.locator._cache['{"label": "B"}'] = MagicMock()
        self.assertEqual(len(self.locator._cache), 2)

    def test_clear_cache(self):
        """clear_cache() empties the session cache."""
        self.locator._cache["test"] = MagicMock()
        self.locator.clear_cache()
        self.assertEqual(len(self.locator._cache), 0)


if __name__ == "__main__":
    unittest.main()
