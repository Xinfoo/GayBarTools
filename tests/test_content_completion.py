"""Fast unit coverage for the page-completion settle window."""

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserSession, ContentTask
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


class ContentCompletionTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(
            AppSettings(poll_interval=2.0),
            AppPaths.for_root(Path("/tmp/unused-content-completion")),
            lambda _message: None,
            threading.Event(),
        )
        self.session._page = Mock()
        self.session._guard = Mock()
        self.waits = []
        self.session._wait = Mock(side_effect=self.waits.append)
        self.session._unresolved_content = Mock(return_value=False)
        self.session._text_control = Mock(return_value=None)
        self.session._read_document_targets = Mock()

    def task(self, identity, order, *, completed=False):
        return ContentTask("document", Mock(), identity, (order, 0, (order,)),
                           completed=completed)

    def test_completed_page_uses_short_settle_instead_of_poll_interval(self):
        done = self.task("document:1", 0, completed=True)
        with patch.object(self.session, "content_tasks",
                          side_effect=[[done], [done], [done], [done]]), \
                patch.object(self.session, "_task_skip_reason", return_value=""):
            executed = self.session.process_content()

        self.assertEqual(executed, 0)
        self.assertEqual(self.waits, [self.session.CONTENT_SETTLE] * 2)
        self.assertNotIn(2.0, self.waits)
        self.assertLess(sum(self.waits), 1.5)

    def test_new_task_found_in_final_scan_is_still_processed(self):
        first = self.task("document:1", 0, completed=True)
        late = self.task("document:2", 1)
        self.session._task_skip_reason = Mock(
            side_effect=lambda target: "任务点已完成" if target is first.target else "")
        snapshots = [
            [first],
            [first],
            [first],
            [first, late],
            [first, late],
            [first, late],
            [first, late],
            [first, late],
        ]
        with patch.object(self.session, "content_tasks", side_effect=snapshots):
            executed = self.session.process_content()

        self.assertEqual(executed, 1)
        self.session._read_document_targets.assert_called_once_with([late.target])
        self.assertEqual(self.waits, [self.session.CONTENT_SETTLE] * 4)
        self.assertLess(sum(self.waits), 2.5)


if __name__ == "__main__":
    unittest.main()
