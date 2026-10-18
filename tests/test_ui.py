"""Exercise start availability and command validation without a Tk display."""

import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths
from gaybar_tools.ui import Application


class Value:
    """The get/set interface used by Tk variables, without creating Tcl."""

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class StartButtonTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="gaybar-ui-test-", dir="/tmp")
        self.addCleanup(folder.cleanup)
        self.app = Application.__new__(Application)
        self.app.paths = AppPaths.for_root(Path(folder.name)).prepare()
        self.app.settings = AppSettings(model="test-model")
        self.app.root = Mock()
        self.app.worker = Mock()
        self.app.demo_url = None
        self.app.busy = self.app.ready = self.app.closing = False
        self.app.events = queue.Queue()
        self.app.state = Value("")
        self.app.url = Value("")
        self.app.logs = Mock()
        self.app.fields = {
            name: Value(getattr(self.app.settings, name))
            for name in (
                "base_url", "model", "api_key", "vision", "ocr",
                "remember_key", "browser_path",
                "max_chapters",
            )
        }
        for name in ("launch", "start", "stop", "test", "save", "save_login"):
            setattr(self.app, name + "_button", Mock())

    def test_start_is_enabled_before_a_chapter_is_detected(self):
        self.app.update_buttons()
        self.app.start_button.configure.assert_called_with(state="normal")

    def test_readiness_loss_does_not_disable_start(self):
        for ready in (True, False):
            with self.subTest(ready=ready):
                self.app.events.put(("ready", ready))
                self.app.drain()
                self.assertEqual(self.app.ready, ready)
                self.app.start_button.configure.assert_called_with(state="normal")

    def test_start_remains_disabled_during_work_or_shutdown(self):
        for busy, closing in ((True, False), (False, True), (True, True)):
            with self.subTest(busy=busy, closing=closing):
                self.app.busy, self.app.closing = busy, closing
                self.app.update_buttons()
                self.app.start_button.configure.assert_called_with(state="disabled")
                self.app.send("start")
                self.app.worker.command.assert_not_called()

    def test_start_queues_valid_settings_even_without_ready_signal(self):
        self.app.fields["model"].set("  chosen-model  ")
        self.app.fields["max_chapters"].set("5")
        with patch("gaybar_tools.ui.messagebox.showerror") as showerror:
            self.app.send("start")
        showerror.assert_not_called()
        self.app.worker.command.assert_called_once()
        name, settings = self.app.worker.command.call_args.args
        self.assertEqual(name, "start")
        self.assertEqual(settings.model, "chosen-model")
        self.assertEqual(settings.max_chapters, 5)
        self.assertIsNot(settings, self.app.settings)
        self.assertTrue(self.app.busy)
        self.app.start_button.configure.assert_called_with(state="disabled")

    def test_start_still_requires_valid_configuration(self):
        cases = (
            ({"model": ""}, "模型名称"),
            ({"vision": False, "ocr": False}, "截图视觉识别或本机 OCR"),
            ({"max_chapters": "0"}, "max_chapters"),
        )
        for values, error_fragment in cases:
            with self.subTest(values=values):
                originals = {name: self.app.fields[name].get() for name in values}
                for name, value in values.items():
                    self.app.fields[name].set(value)
                with patch("gaybar_tools.ui.messagebox.showerror") as showerror:
                    self.app.send("start")
                self.assertIn(error_fragment, showerror.call_args.args[1])
                self.app.worker.command.assert_not_called()
                self.assertFalse(self.app.busy)
                for name, value in originals.items():
                    self.app.fields[name].set(value)

    def test_save_login_does_not_require_api_configuration(self):
        self.app.fields["model"].set("")
        self.app.fields["base_url"].set("unfinished API address")
        with patch("gaybar_tools.ui.messagebox.showerror") as showerror:
            self.app.send("save_login")
        showerror.assert_not_called()
        self.assertEqual(self.app.worker.command.call_args.args[0], "save_login")
        self.app.save_login_button.configure.assert_called_with(state="disabled")

    def test_save_login_is_blocked_while_busy_or_closing(self):
        for busy, closing in ((True, False), (False, True)):
            self.app.busy, self.app.closing = busy, closing
            self.app.update_buttons()
            self.app.save_login_button.configure.assert_called_with(state="disabled")
            self.app.send("save_login")
            self.app.worker.command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
