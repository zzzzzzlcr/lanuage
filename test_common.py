"""Unit tests for CDPHelper — especially JSON double-encoding in eval()."""
import unittest
from unittest.mock import patch, MagicMock
import json, sys, os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from common import CDPHelper


def cdp_out(value):
    """Simulate CDP binary output: JSON-encode the value, adding outer quotes + newline.
    CDP wraps the JS return value in JSON quotes. For strings, inner quotes get escaped."""
    if isinstance(value, str) and not value.startswith('"'):
        # Plain JS string → CDP wraps in JSON quotes
        return json.dumps(value) + '\n'
    # Already formatted (JSON array/object string from JSON.stringify)
    return json.dumps(value) + '\n'


class TestEvalDecoding(unittest.TestCase):
    """Verify eval() correctly decodes CDP binary double-encoded output."""

    def _make_cdp(self):
        cdp = CDPHelper.__new__(CDPHelper)
        cdp.host = "127.0.0.1"
        cdp.port = "9222"
        return cdp

    @patch('common.subprocess.run')
    def test_number_output(self, mock_run):
        """42 → CDP outputs '"42"' → eval returns '42' (str, type lost in encoding)."""
        cdp = self._make_cdp()
        mock_run.return_value = MagicMock(stdout='"42"\n', stderr='')
        self.assertEqual(cdp.eval("return 42"), '42')

    @patch('common.subprocess.run')
    def test_string_output(self, mock_run):
        """'hello' → CDP outputs '"hello"' → eval returns 'hello'."""
        cdp = self._make_cdp()
        mock_run.return_value = MagicMock(stdout='"hello"\n', stderr='')
        self.assertEqual(cdp.eval("return 'hello'"), "hello")

    @patch('common.subprocess.run')
    def test_json_array_output(self, mock_run):
        """JSON.stringify([1,2,3]) → CDP double-encodes → eval returns [1,2,3]."""
        cdp = self._make_cdp()
        # JS returns the string '[1,2,3]', CDP wraps it: '"[\"1,2,3\"]"'
        raw = json.dumps(json.dumps([1, 2, 3])) + '\n'
        mock_run.return_value = MagicMock(stdout=raw, stderr='')
        self.assertEqual(cdp.eval("return JSON.stringify([1,2,3])"), [1, 2, 3])

    @patch('common.subprocess.run')
    def test_json_object_output(self, mock_run):
        """JSON.stringify({...}) → CDP double-encodes → eval returns dict."""
        cdp = self._make_cdp()
        obj_str = json.dumps({"tag": "INPUT", "type": "text"})
        raw = json.dumps(obj_str) + '\n'
        mock_run.return_value = MagicMock(stdout=raw, stderr='')
        result = cdp.eval("return JSON.stringify({tag:'INPUT'})")
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get('tag'), 'INPUT')

    @patch('common.subprocess.run')
    def test_single_element_array(self, mock_run):
        """JSON.stringify(['state']) → CDP double-encodes → eval returns ['state']."""
        cdp = self._make_cdp()
        inner = json.dumps(['state'])  # '["state"]'
        raw = json.dumps(inner) + '\n'  # '"[\"state\"]"'
        mock_run.return_value = MagicMock(stdout=raw, stderr='')
        result = cdp.eval("return JSON.stringify(['state'])")
        self.assertIsInstance(result, list)
        self.assertEqual(result, ['state'])

    @patch('common.subprocess.run')
    def test_empty_array(self, mock_run):
        """JSON.stringify([]) → CDP double-encodes → eval returns []."""
        cdp = self._make_cdp()
        inner = json.dumps([])
        raw = json.dumps(inner) + '\n'
        mock_run.return_value = MagicMock(stdout=raw, stderr='')
        self.assertEqual(cdp.eval("return JSON.stringify([])"), [])

    @patch('common.subprocess.run')
    def test_browser_error_passthrough(self, mock_run):
        """Browser error should NOT be JSON-decoded."""
        cdp = self._make_cdp()
        mock_run.return_value = MagicMock(stdout='', stderr='failed to create client')
        self.assertIn("ERROR", cdp.eval("anything"))

    @patch('common.subprocess.run')
    def test_unparseable_returns_raw(self, mock_run):
        """Non-JSON output is returned as-is."""
        cdp = self._make_cdp()
        mock_run.return_value = MagicMock(stdout='raw text\n', stderr='')
        self.assertEqual(cdp.eval("something"), "raw text")


class TestFormMethod(unittest.TestCase):
    """Verify form() handles None values correctly."""

    @patch('common.subprocess.run')
    def test_form_select_only_no_value_flag(self, mock_run):
        """value=None + select set → no --value flag passed."""
        cdp = CDPHelper.__new__(CDPHelper)
        cdp.host, cdp.port = "127.0.0.1", "9222"
        mock_run.return_value = MagicMock(stdout='ok', stderr='')
        cdp.form("#sel", value=None, select="2020")
        args = mock_run.call_args[0][0]
        self.assertIn("--select", args)
        self.assertNotIn("--value", args)

    @patch('common.subprocess.run')
    def test_form_value_only_no_select_flag(self, mock_run):
        """select=None + value set → no --select flag passed."""
        cdp = CDPHelper.__new__(CDPHelper)
        cdp.host, cdp.port = "127.0.0.1", "9222"
        mock_run.return_value = MagicMock(stdout='ok', stderr='')
        cdp.form("#inp", value="hello", select=None)
        args = mock_run.call_args[0][0]
        self.assertIn("--value", args)
        self.assertNotIn("--select", args)


class TestNavigate(unittest.TestCase):
    """Verify navigate uses cdp navi command not eval."""

    @patch('common.subprocess.run')
    def test_navigate_uses_navi(self, mock_run):
        """navigate() should use 'navi' not 'eval'."""
        cdp = CDPHelper.__new__(CDPHelper)
        cdp.host, cdp.port = "127.0.0.1", "9222"
        mock_run.return_value = MagicMock(stdout='ok', stderr='')
        cdp.navigate("https://example.com")
        args = mock_run.call_args[0][0]
        self.assertIn("navi", args)
        self.assertNotIn("eval", args)


if __name__ == "__main__":
    unittest.main()
