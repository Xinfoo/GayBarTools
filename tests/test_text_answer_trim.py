"""Fast unit coverage for removing a rich editor's structural trailing break."""

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from gaybar_tools.browser import BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


class TextAnswerTrimTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(
            AppSettings(),
            AppPaths.for_root(Path("/tmp/unused-text-answer-trim")),
            lambda _message: None,
            threading.Event(),
        )
        self.session._page = Mock()
        self.control = Mock()

    def test_structural_trailing_break_is_removed_with_keyboard(self):
        actual = {"text": "答案\n", "rich": True}
        self.control.evaluate.side_effect = [True, {"text": "答案", "rich": True}]

        result = self.session._trim_rich_text_tail(self.control, actual)

        self.assertEqual(result, {"text": "答案", "rich": True})
        self.session._page.keyboard.press.assert_any_call("ControlOrMeta+End", timeout=3000)
        self.session._page.keyboard.press.assert_any_call("Backspace", timeout=3000)
        self.assertEqual(self.session._page.keyboard.press.call_count, 2)

    def test_internal_and_missing_trailing_newlines_are_untouched(self):
        for actual in (
            {"text": "答案", "rich": True},
            {"text": "第一行\n第二行", "rich": True},
        ):
            with self.subTest(actual=actual):
                self.control.evaluate.reset_mock()
                result = self.session._trim_rich_text_tail(self.control, actual)
                self.assertEqual(result, actual)
                self.control.evaluate.assert_not_called()
                self.session._page.keyboard.press.assert_not_called()

    def test_native_textarea_is_never_trimmed(self):
        actual = {"text": "答案\n", "rich": False}
        result = self.session._trim_rich_text_tail(self.control, actual)
        self.assertEqual(result, actual)
        self.session._page.keyboard.press.assert_not_called()

    def test_block_boundary_newline_without_structural_tail_is_kept(self):
        actual = {"text": "答案\n", "rich": True}
        self.control.evaluate.side_effect = [False]
        result = self.session._trim_rich_text_tail(self.control, actual)
        self.assertEqual(result, actual)
        self.session._page.keyboard.press.assert_not_called()

    def test_multiple_empty_paragraphs_are_removed_one_at_a_time(self):
        actual = {"text": "答案\n\n", "rich": True}
        self.control.evaluate.side_effect = [
            True, {"text": "答案\n", "rich": True},
            True, {"text": "答案", "rich": True},
        ]
        result = self.session._trim_rich_text_tail(self.control, actual)
        self.assertEqual(result, {"text": "答案", "rich": True})
        self.assertEqual(self.session._page.keyboard.press.call_count, 4)


if __name__ == "__main__":
    unittest.main()
