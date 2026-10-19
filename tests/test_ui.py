"""Exercise desktop controls and worker events without a display or browser."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (QApplication, QCheckBox, QLineEdit, QScrollArea,
                               QStyle, QStyleOptionFrame)

from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths
from gaybar_tools.ui import Application


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="gaybar-ui-test-", dir="/tmp")
        self.addCleanup(folder.cleanup)
        self.worker = Mock()
        self.worker.is_alive.return_value = True
        with patch("gaybar_tools.ui.Worker", return_value=self.worker):
            self.app = Application(
                AppPaths.for_root(Path(folder.name)).prepare(),
                AppSettings(model="test-model"),
            )
        self.addCleanup(self.dispose_window)

    def dispose_window(self):
        self.worker.is_alive.return_value = False
        self.app.close()
        self.app.drain()
        self.app.deleteLater()
        self.qt.processEvents()

    def set_field(self, name, value):
        field = self.app.fields[name]
        if isinstance(field, QCheckBox):
            field.setChecked(value)
        else:
            field.setText(str(value))

    def get_field(self, name):
        field = self.app.fields[name]
        return field.isChecked() if isinstance(field, QCheckBox) else field.text()

    def test_start_is_enabled_before_a_chapter_is_detected(self):
        self.app.update_buttons()
        self.assertTrue(self.app.start_button.isEnabled())

    def test_short_windows_preserve_line_edit_text_and_content_access(self):
        self.app.show()
        for height in (900, 800, 720):
            with self.subTest(height=height):
                self.app.resize(1200, height)
                self.qt.processEvents()
                for name, field in self.app.fields.items():
                    if not isinstance(field, QLineEdit):
                        continue
                    option = QStyleOptionFrame()
                    field.initStyleOption(option)
                    text_rect = field.style().subElementRect(
                        QStyle.SubElement.SE_LineEditContents, option, field,
                    )
                    self.assertGreaterEqual(text_rect.height(), field.fontMetrics().height(), name)
                    self.assertTrue(field.parentWidget().rect().contains(field.geometry()), name)
                scroll = self.app.findChild(QScrollArea)
                scroll.ensureWidgetVisible(self.app.logs)
                origin = self.app.logs.mapTo(scroll.viewport(), self.app.logs.rect().topLeft())
                log_rect = self.app.logs.rect().translated(origin)
                self.assertTrue(scroll.viewport().rect().intersects(log_rect))
                scroll.verticalScrollBar().setValue(0)

    def test_readiness_loss_does_not_disable_start(self):
        for ready in (True, False):
            with self.subTest(ready=ready):
                self.app.events.put(("ready", ready))
                self.app.drain()
                self.assertEqual(self.app.ready, ready)
                self.assertTrue(self.app.start_button.isEnabled())

    def test_start_remains_disabled_during_work_or_shutdown(self):
        for busy, closing in ((True, False), (False, True), (True, True)):
            with self.subTest(busy=busy, closing=closing):
                self.app.busy, self.app.closing = busy, closing
                self.app.update_buttons()
                self.assertFalse(self.app.start_button.isEnabled())
                self.app.send("start")
                self.worker.command.assert_not_called()

    def test_start_queues_valid_settings_even_without_ready_signal(self):
        self.set_field("model", "  chosen-model  ")
        self.set_field("max_chapters", "5")
        with patch("gaybar_tools.ui.QMessageBox.critical") as showerror:
            self.app.send("start")
        showerror.assert_not_called()
        self.worker.command.assert_called_once()
        name, settings = self.worker.command.call_args.args
        self.assertEqual(name, "start")
        self.assertEqual(settings.model, "chosen-model")
        self.assertEqual(settings.max_chapters, 5)
        self.assertIsNot(settings, self.app.settings)
        self.assertTrue(self.app.busy)
        self.assertFalse(self.app.start_button.isEnabled())

    def test_start_still_requires_valid_configuration(self):
        cases = (
            ({"model": ""}, "模型名称"),
            ({"vision": False, "ocr": False}, "截图视觉识别或本机 OCR"),
            ({"max_chapters": "0"}, "max_chapters"),
        )
        for values, error_fragment in cases:
            with self.subTest(values=values):
                originals = {name: self.get_field(name) for name in values}
                for name, value in values.items():
                    self.set_field(name, value)
                with patch("gaybar_tools.ui.QMessageBox.critical") as showerror:
                    self.app.send("start")
                self.assertIn(error_fragment, showerror.call_args.args[2])
                self.worker.command.assert_not_called()
                self.assertFalse(self.app.busy)
                for name, value in originals.items():
                    self.set_field(name, value)

    def test_save_login_does_not_require_api_configuration(self):
        self.set_field("model", "")
        self.set_field("base_url", "unfinished API address")
        with patch("gaybar_tools.ui.QMessageBox.critical") as showerror:
            self.app.send("save_login")
        showerror.assert_not_called()
        self.assertEqual(self.worker.command.call_args.args[0], "save_login")
        self.assertFalse(self.app.save_login_button.isEnabled())

    def test_save_login_is_blocked_while_busy_or_closing(self):
        for busy, closing in ((True, False), (False, True)):
            with self.subTest(busy=busy, closing=closing):
                self.app.busy, self.app.closing = busy, closing
                self.app.update_buttons()
                self.assertFalse(self.app.save_login_button.isEnabled())
                self.app.send("save_login")
                self.worker.command.assert_not_called()

    def test_worker_events_update_logs_status_and_redacted_url(self):
        for event in (
            ("log", "第一条日志"),
            ("log", "第二条日志"),
            ("state", "正在处理章节"),
            ("url", "https://example.test/chapter?token=private&chapter=1"),
            ("busy", True),
        ):
            self.app.events.put(event)
        self.app.drain()
        self.assertEqual(self.app.logs.toPlainText(), "第一条日志\n第二条日志")
        self.assertTrue(self.app.logs.isReadOnly())
        self.assertEqual(self.app.state.text(), "正在处理章节")
        self.assertEqual(self.app.url.text(), "https://example.test/chapter")
        self.assertTrue(self.app.busy)
        self.assertFalse(self.app.start_button.isEnabled())

        self.app.events.put(("busy", False))
        self.app.events.put(("url", ""))
        self.app.drain()
        self.assertTrue(self.app.start_button.isEnabled())
        self.assertEqual(self.app.url.text(), "尚未打开浏览器")

    def test_close_waits_for_worker_without_blocking_event_loop(self):
        event = QCloseEvent()
        self.app.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.app.closing)
        self.worker.stop.assert_called_once_with()
        self.worker.command.assert_called_once_with("shutdown")
        self.assertFalse(self.app.start_button.isEnabled())
        self.assertTrue(self.app.timer.isActive())

        self.app.closeEvent(QCloseEvent())
        self.app.drain()
        self.worker.command.assert_called_once_with("shutdown")
        self.assertTrue(self.app.timer.isActive())

        self.worker.is_alive.return_value = False
        with patch.object(self.app, "close", wraps=self.app.close) as close:
            self.app.drain()
        close.assert_called_once_with()
        self.assertFalse(self.app.timer.isActive())
        event = QCloseEvent()
        self.app.closeEvent(event)
        self.assertTrue(event.isAccepted())


class ProcessShutdownTests(unittest.TestCase):
    def test_main_exits_without_crashing_during_pyside_cleanup(self):
        # A long-lived QApplication in this test process hides teardown crashes.
        script = textwrap.dedent("""
            import tempfile
            from pathlib import Path
            from unittest.mock import patch
            from PySide6.QtCore import QTimer
            from gaybar_tools.__main__ import main
            from gaybar_tools.config import AppSettings
            from gaybar_tools.paths import AppPaths
            from gaybar_tools.ui import Application

            class ClosingWindow(Application):
                def __init__(self, *args):
                    super().__init__(*args)
                    QTimer.singleShot(20, self.close)

            with tempfile.TemporaryDirectory(prefix='gaybar-exit-test-') as folder:
                paths = AppPaths.for_root(Path(folder)).prepare()
                with patch('gaybar_tools.__main__.AppPaths.for_root', return_value=paths), \\
                        patch('gaybar_tools.__main__.AppSettings.load', return_value=AppSettings()), \\
                        patch('gaybar_tools.ui.Application', ClosingWindow):
                    result = main([])
                assert result == 0, result
        """)
        result = subprocess.run(
            [sys.executable, "-B", "-X", "faulthandler", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
