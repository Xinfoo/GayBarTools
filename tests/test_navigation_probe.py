"""Unit coverage for the cheap navigation probes and per-poll caches."""

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserSession, ContentTask
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


class NavigationProbeTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(
            AppSettings(),
            AppPaths.for_root(Path("/tmp/unused-navigation-probe")),
            lambda _message: None,
            threading.Event(),
        )
        self.session._navigation_mode = True

    def tearDown(self):
        self.session._navigation_mode = False
        self.session._reset_navigation_caches()

    def test_cached_content_tasks_are_reused_until_the_poll_resets(self):
        task = ContentTask("document", Mock(), "document:1", (0, 0, (0,)))
        with patch.object(self.session, "_content_tasks_uncached",
                          return_value=[task]) as uncached:
            first = self.session.content_tasks(_cached=True)
            second = self.session.content_tasks(_cached=True)
            self.assertIs(first, second)
            self.assertEqual(uncached.call_count, 1)

            self.session._reset_navigation_caches()
            self.session.content_tasks(_cached=True)
            self.assertEqual(uncached.call_count, 2)

    def test_quiz_rows_are_built_from_one_probe_call(self):
        probe = {
            "rows": [["q1", "1【单选题】题目", "choice", 2, ["A. 一", "B. 二"], 1]],
            "ready": True,
            "signature": "digest",
        }
        with patch.object(self.session, "_quiz_probe", return_value=probe) as scan, \
                patch.object(self.session, "_question_blocks",
                             side_effect=AssertionError("不应逐题读取 DOM")):
            rows, ready = self.session._quiz_rows(Mock())

        self.assertEqual(rows, [("q1", "1【单选题】题目", ["A. 一", "B. 二"])])
        self.assertTrue(ready)
        scan.assert_called_once()

    def test_navigation_has_quiz_uses_the_compact_probe(self):
        frame = Mock()
        with patch.object(self.session, "_frames", return_value=[frame]), \
                patch.object(self.session, "_frame_visible_memo", return_value=True), \
                patch.object(self.session, "_quiz_probe",
                             return_value={"rows": [["q1"]]}) as probe, \
                patch.object(self.session, "_question_blocks",
                             side_effect=AssertionError("不应逐题读取 DOM")):
            self.assertTrue(self.session._has_quiz())

        probe.assert_called_once_with(frame)

    def test_standalone_quiz_uses_one_body_target(self):
        frame = Mock()
        body = Mock()
        frame.locator.return_value = body
        with patch.object(self.session, "_frames", return_value=[frame]), \
                patch.object(self.session, "_frame_visible_memo", return_value=True), \
                patch.object(self.session, "_quiz_probe",
                             return_value={"rows": [["q1"]]}), \
                patch.object(self.session, "_result_probe_cached", return_value=False), \
                patch.object(self.session, "_root_has_task_cards", return_value=False), \
                patch.object(self.session, "_content_identity", return_value="quiz:1"):
            targets = self.session._chapter_quiz_targets_fast()

        self.assertEqual(targets, [body])


if __name__ == "__main__":
    unittest.main()
