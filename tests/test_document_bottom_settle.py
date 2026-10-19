"""Fast unit coverage for the shortened document-bottom confirmation."""

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


class DocumentBottomSettleTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        self.waits = []
        self.session = BrowserSession(
            AppSettings(poll_interval=2.0),
            AppPaths.for_root(Path("/tmp/unused-document-bottom")),
            lambda _message: None,
            threading.Event(),
        )
        self.session._guard = Mock()
        self.session._page = Mock()
        self.session._task_skip_reason = Mock(return_value="")
        self.session._content_identity = Mock(return_value="document:1")
        self.session._document_pointer = Mock(
            return_value=(10.0, 20.0, {"width": 100.0, "height": 100.0}))

        def wait(seconds):
            self.waits.append(seconds)
            self.clock[0] += seconds

        self.session._wait = Mock(side_effect=wait)
        self.target = Mock()

    def metrics(self, height):
        return {"top": height - 100.0, "client": 100.0, "height": height}

    def test_bottom_uses_short_fixed_wait_and_three_reads(self):
        self.session._scroll_metrics = Mock(return_value=self.metrics(1000.0))
        with patch("gaybar_tools.browser.time.monotonic", side_effect=lambda: self.clock[0]):
            self.session._read_document_targets([self.target])

        self.assertEqual(self.waits, [self.session.DOCUMENT_BOTTOM_WAIT] * 3)
        self.assertLess(sum(self.waits), 2.0)
        self.assertNotIn(2.0, self.waits)
        self.assertEqual(self.session._document_pointer.call_count, 4)

    def test_height_growth_resets_bottom_confirmation(self):
        heights = [1000.0, 1000.0, 1200.0, 1200.0, 1200.0, 1200.0, 1200.0]
        index = {"value": 0}

        def metrics(_target):
            value = heights[min(index["value"], len(heights) - 1)]
            index["value"] += 1
            return self.metrics(value)

        self.session._scroll_metrics = Mock(side_effect=metrics)
        with patch("gaybar_tools.browser.time.monotonic", side_effect=lambda: self.clock[0]):
            self.session._read_document_targets([self.target])

        self.assertGreaterEqual(len(self.waits), 5)
        self.assertTrue(all(seconds == self.session.DOCUMENT_BOTTOM_WAIT
                            for seconds in self.waits))


if __name__ == "__main__":
    unittest.main()
