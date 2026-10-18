"""Fast unit coverage for the non-humanized next-navigation click."""

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from gaybar_tools.browser import BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


class NavigationFastClickTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(
            AppSettings(),
            AppPaths.for_root(Path("/tmp/unused-navigation-fast-click")),
            lambda _message: None,
            threading.Event(),
        )
        self.session._page = Mock()
        self.session._guard = Mock()
        self.session._visible = Mock(return_value=True)
        self.session._move_mouse = Mock()
        self.session._delay = Mock()
        self.locator = Mock()
        self.locator.get_attribute.return_value = None
        self.locator.bounding_box.return_value = {
            "x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0,
        }

    def test_fast_click_skips_humanized_delays_but_keeps_mouse_input(self):
        self.session._click(self.locator, humanized=False)

        self.session._delay.assert_not_called()
        self.session._move_mouse.assert_called_once()
        self.assertEqual(self.session._page.mouse.click.call_args.kwargs["delay"], 0)
        self.locator.click.assert_called_once_with(trial=True, timeout=3000)

    def test_default_click_keeps_humanized_delays(self):
        self.session._click(self.locator)

        self.assertEqual(self.session._delay.call_count, 2)
        self.assertGreater(self.session._page.mouse.click.call_args.kwargs["delay"], 0)


if __name__ == "__main__":
    unittest.main()
