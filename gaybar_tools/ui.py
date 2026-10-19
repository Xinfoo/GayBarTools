"""Qt desktop interface; browser and HTTP work stay on the worker thread."""

import queue
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from .config import AppSettings
from .paths import AppPaths
from .worker import Worker


class Application(QWidget):
    def __init__(self, paths: AppPaths, settings: AppSettings,
                 demo_url: str | None = None):
        super().__init__()
        # Delete widgets while Qt is running, before Python/PySide exit cleanup.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.paths, self.settings, self.demo_url = paths, settings, demo_url
        self.events: queue.Queue = queue.Queue()
        self.worker = Worker(paths, self.events, demo_url)
        self.busy = self.ready = self.closing = self._shutdown_complete = False
        self.fields: dict[str, QLineEdit | QCheckBox] = {}
        self.background = QPixmap(str(Path(__file__).with_name("assets") / "background.png"))
        self.setWindowTitle("GayBarTools · 学习通章节助手" + (" · 本地演示" if demo_url else ""))
        self.build()
        available = self.screen().availableGeometry()
        side = min(900, available.width() - 80, available.height() - 100)
        side = max(100, side // 100 * 100)
        self.resize(side, side)
        self.setMinimumSize(min(820, side), min(720, side))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(100)
        self.worker.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#141020"))
        if not self.background.isNull():
            scale = max(self.width() / self.background.width(), self.height() / self.background.height())
            source = QRectF(0, 0, self.width() / scale, self.height() / scale)
            source.moveCenter(QRectF(self.background.rect()).center())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(QRectF(self.rect()), self.background, source)
            painter.fillRect(self.rect(), QColor(0, 0, 0, 115))
        painter.end()

    def build(self):
        self.setStyleSheet("""
            QWidget { color: white; font-size: 14px; }
            QLabel { background: transparent; }
            QLabel#title { font-size: 32px; font-weight: bold; }
            QGroupBox { border: 1px solid #a99cb5; margin-top: 12px; padding: 16px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; }
            QLineEdit { background: #faf9fc; color: #1b1730; padding: 6px;
                        border: 1px solid #b6adbf; border-radius: 3px; }
            QCheckBox { background: rgba(20, 16, 32, 160); padding: 4px; }
            QPushButton { background: #e7e2ec; color: #21182d; padding: 10px 14px;
                          border: 1px solid #b6adbf; border-radius: 3px; }
            QPushButton:hover { background: #f7f1ff; }
            QPushButton:disabled { background: #c6c1cc; color: #77717d; }
            QPushButton#start { background: #1765bb; color: white; }
            QPushButton#start:disabled { background: #74859b; color: #d5d9df; }
            QPlainTextEdit { background: #10233d; color: #d9e6f4; padding: 10px;
                             border: 1px solid #b6adbf; font-family: monospace; }
            QScrollArea { background: transparent; border: none; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel("GayBarTools", objectName="title")
        layout.addWidget(title)
        layout.addWidget(QLabel("章节学习助手  /  独立浏览器会话  /  正常播放与阅读"))

        provider = QGroupBox("模型与识别")
        form = QGridLayout(provider)
        form.setColumnStretch(1, 1)
        self.entry(form, 0, "API 地址", "base_url")
        self.entry(form, 1, "模型名称", "model")
        self.entry(form, 2, "API Key", "api_key")
        self.fields["api_key"].setEchoMode(QLineEdit.EchoMode.Password)
        flags = QHBoxLayout()
        for text, name in (("发送检测页截图", "vision"), ("本机 OCR", "ocr"),
                           ("记住密钥（本地明文）", "remember_key")):
            flags.addWidget(self.check(text, name))
        flags.addStretch()
        form.addLayout(flags, 3, 0, 1, 3)
        form.addWidget(QLabel("开始作答后，题目及勾选的截图会发送至上方 API 地址。"), 4, 0, 1, 3)
        layout.addWidget(provider)

        runtime = QGroupBox("运行设置")
        form = QGridLayout(runtime)
        form.setColumnStretch(1, 1)
        self.entry(form, 0, "浏览器路径", "browser_path")
        form.addWidget(self.button("选择…", self.browse), 0, 2)
        form.addWidget(QLabel("留空自动查找 Chromium / Edge / Chrome；会话保存在 .runtime。"), 1, 0, 1, 3)
        options = QHBoxLayout()
        options.addWidget(QLabel("学习页面上限"))
        self.fields["max_chapters"] = QLineEdit(str(self.settings.max_chapters))
        self.fields["max_chapters"].setFixedWidth(80)
        options.addWidget(self.fields["max_chapters"])
        options.addStretch()
        form.addLayout(options, 2, 0, 1, 3)
        login = QHBoxLayout()
        login.addWidget(QLabel("登录状态自动保存在项目中，下次打开浏览器时恢复。"))
        login.addStretch()
        self.save_login_button = self.button("立即保存登录状态", lambda: self.send("save_login"))
        login.addWidget(self.save_login_button)
        form.addLayout(login, 3, 0, 1, 3)
        layout.addWidget(runtime)

        actions = QHBoxLayout()
        self.launch_button = self.button("1  打开浏览器", lambda: self.send("launch"))
        self.start_button = self.button("2  开始刷课", lambda: self.send("start"))
        self.start_button.setObjectName("start")
        self.stop_button = self.button("停止", self.stop)
        self.stop_button.setEnabled(False)
        for button in (self.launch_button, self.start_button, self.stop_button):
            actions.addWidget(button)
        actions.addStretch()
        self.save_button = self.button("保存设置", self.save)
        self.test_button = self.button("测试 API", lambda: self.send("test"))
        actions.addWidget(self.save_button)
        actions.addWidget(self.test_button)
        layout.addLayout(actions)
        self.state = QLabel("本地演示 · 无需账号或 API" if self.demo_url else "打开浏览器，登录后进入任意课程章节")
        self.state.setStyleSheet("color: #8eeaff;")
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        self.url = QLabel("尚未打开浏览器")
        self.url.setWordWrap(True)
        self.url.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.state)
        layout.addWidget(self.url)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMinimumHeight(self.logs.fontMetrics().lineSpacing() * 4 + 24)
        # Fill spare space without forcing the scroll page to the log's size hint.
        self.logs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.logs, 1)
        footer = QLabel("遇到未识别页面、答案格式重试耗尽或提交异常会停止，并保存诊断截图。")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        scroll.setWidget(content)
        content.setAutoFillBackground(False)
        self.update_buttons()

    def entry(self, layout, row, label, name):
        layout.addWidget(QLabel(label), row, 0)
        self.fields[name] = QLineEdit(getattr(self.settings, name))
        layout.addWidget(self.fields[name], row, 1, 1, 1 if name == "browser_path" else 2)

    def check(self, label, name):
        field = self.fields[name] = QCheckBox(label)
        field.setChecked(getattr(self.settings, name))
        return field

    @staticmethod
    def button(text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        return button

    def browse(self):
        selected, _ = QFileDialog.getOpenFileName(self, "选择浏览器可执行文件")
        if selected:
            self.fields["browser_path"].setText(selected)

    def collect(self, require_model=False):
        values = {key: field.isChecked() if isinstance(field, QCheckBox) else field.text()
                  for key, field in self.fields.items()}
        values["max_chapters"] = int(values["max_chapters"])
        for key in ("base_url", "model", "browser_path"):
            values[key] = values[key].strip()
        settings = replace(self.settings, **values)
        settings.validate(require_model=require_model and not self.demo_url)
        if require_model and not self.demo_url and not (settings.vision or settings.ocr):
            raise ValueError("请至少开启截图视觉识别或本机 OCR。")
        return settings

    def save(self):
        try:
            self.settings = self.collect()
            self.settings.save(self.paths.runtime / "settings.json")
            self.state.setText("设置已保存至项目 .runtime/settings.json")
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, "设置无效", str(exc))

    def send(self, name):
        if self.busy or self.closing:
            return
        try:
            # Saving the browser session also works before configuring an API.
            if name != "save_login":
                self.settings = self.collect(require_model=name in {"start", "test"})
                self.settings.save(self.paths.runtime / "settings.json")
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, "设置无效", str(exc))
            return
        self.busy = True
        self.update_buttons()
        self.state.setText({"launch": "正在打开浏览器…", "start": "开始处理课程章节…",
                            "test": "正在测试 API…", "save_login": "正在保存登录状态…"}[name])
        self.worker.command(name, replace(self.settings))

    def stop(self):
        self.worker.stop()
        self.state.setText("正在停止；正在进行的网络请求会等待返回或超时。")
        self.stop_button.setEnabled(False)

    def update_buttons(self):
        idle = not self.busy and not self.closing
        self.launch_button.setEnabled(idle)
        # The worker validates the page when Start is clicked; readiness is advisory.
        self.start_button.setEnabled(idle)
        self.stop_button.setEnabled(self.busy and not self.closing)
        self.test_button.setEnabled(idle and not self.demo_url)
        self.save_button.setEnabled(not self.closing)
        self.save_login_button.setEnabled(idle)

    def drain(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.logs.appendPlainText(value)
                self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())
            elif kind == "state":
                self.state.setText(value)
            elif kind == "url":
                self.url.setText(value.split("?", 1)[0] if value else "尚未打开浏览器")
            elif kind == "ready":
                self.ready = value
                self.update_buttons()
            elif kind == "busy":
                self.busy = value
                self.update_buttons()
        if self.closing and not self.worker.is_alive():
            self._shutdown_complete = True
            self.timer.stop()
            self.close()

    def closeEvent(self, event):
        if self._shutdown_complete:
            event.accept()
            return
        event.ignore()
        if not self.closing:
            self.closing = True
            self.worker.stop()
            self.worker.command("shutdown")
            self.state.setText("正在关闭独立浏览器并保存会话…")
            self.update_buttons()
