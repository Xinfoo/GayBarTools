"""Chinese desktop interface; browser and HTTP work never block Tk."""

import queue
import tkinter as tk
from dataclasses import replace
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .config import AppSettings
from .paths import AppPaths
from .worker import Worker


class Application:
    def __init__(self, root: tk.Tk, paths: AppPaths, settings: AppSettings,
                 demo_url: str | None = None):
        self.root, self.paths, self.settings = root, paths, settings
        self.demo_url = demo_url
        self.events: queue.Queue = queue.Queue()
        self.worker = Worker(paths, self.events, demo_url)
        self.busy = False
        self.ready = False
        self.closing = False
        self.fields: dict[str, tk.Variable] = {}
        self.state = tk.StringVar(value="本地演示 · 无需账号或 API" if demo_url else "打开浏览器，登录后进入任意课程章节")
        self.url = tk.StringVar(value="尚未打开浏览器")
        root.title("GayBarTools · 学习通章节助手" + (" · 本地演示" if demo_url else ""))
        root.geometry("960x820")
        root.minsize(820, 720)
        self.build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.worker.start()
        root.after(100, self.drain)

    def build(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa", font=("sans-serif", 10))
        style.configure("Title.TLabel", font=("sans-serif", 23, "bold"), foreground="#172d51")
        style.configure("Subtitle.TLabel", foreground="#56657b")
        style.configure("TButton", padding=(14, 9))
        style.configure("Start.TButton", background="#1765bb", foreground="white")
        style.configure("TLabelframe", background="#f4f6fa")
        style.configure("TLabelframe.Label", background="#f4f6fa", foreground="#304b70", font=("sans-serif", 10, "bold"))
        self.root.configure(background="#f4f6fa")
        container = ttk.Frame(self.root, padding=24)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="GayBarTools", style="Title.TLabel").pack(anchor="w")
        ttk.Label(container, text="章节学习助手  /  独立浏览器会话  /  正常播放与阅读", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 16))

        provider = ttk.LabelFrame(container, text="模型与识别", padding=14)
        provider.pack(fill="x")
        provider.columnconfigure(1, weight=1)
        self.entry(provider, 0, "API 地址", "base_url")
        self.entry(provider, 1, "模型名称", "model")
        self.entry(provider, 2, "API Key", "api_key", show="•")
        flags = ttk.Frame(provider)
        flags.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.check(flags, "发送检测页截图", "vision").pack(side="left", padx=(0, 14))
        self.check(flags, "本机 OCR", "ocr").pack(side="left", padx=(0, 14))
        self.check(flags, "记住密钥（本地明文）", "remember_key").pack(side="left")
        ttk.Label(provider, text="开始作答后，题目及勾选的截图会发送至上方 API 地址。", style="Subtitle.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        runtime = ttk.LabelFrame(container, text="运行设置", padding=14)
        runtime.pack(fill="x", pady=(12, 0))
        runtime.columnconfigure(1, weight=1)
        self.entry(runtime, 0, "浏览器路径", "browser_path")
        ttk.Button(runtime, text="选择…", command=self.browse).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(runtime, text="留空自动查找 Chromium / Edge / Chrome；会话保存在 .runtime。", style="Subtitle.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 10))
        options = ttk.Frame(runtime)
        options.grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(options, text="置信度下限").pack(side="left")
        self.fields["confidence_threshold"] = tk.StringVar(value=str(self.settings.confidence_threshold))
        ttk.Entry(options, textvariable=self.fields["confidence_threshold"], width=7).pack(side="left", padx=(6, 18))
        ttk.Label(options, text="章节上限").pack(side="left")
        self.fields["max_chapters"] = tk.StringVar(value=str(self.settings.max_chapters))
        ttk.Entry(options, textvariable=self.fields["max_chapters"], width=7).pack(side="left", padx=(6, 18))
        self.check(options, "自动提交并继续下一节", "auto_submit").pack(side="left")

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=16)
        self.launch_button = ttk.Button(actions, text="1  打开浏览器", command=lambda: self.send("launch"))
        self.launch_button.pack(side="left")
        self.start_button = ttk.Button(actions, text="2  开始刷课", style="Start.TButton", command=lambda: self.send("start"), state="disabled")
        self.start_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        self.test_button = ttk.Button(actions, text="测试 API", command=lambda: self.send("test"))
        self.test_button.pack(side="right")
        self.save_button = ttk.Button(actions, text="保存设置", command=self.save)
        self.save_button.pack(side="right", padx=10)
        ttk.Label(container, textvariable=self.state, foreground="#1765bb", wraplength=860).pack(anchor="w")
        ttk.Label(container, textvariable=self.url, style="Subtitle.TLabel", wraplength=860).pack(anchor="w", pady=(4, 10))
        self.logs = scrolledtext.ScrolledText(container, height=9, state="disabled", background="#10233d", foreground="#d9e6f4", relief="flat", font=("monospace", 10), padx=12, pady=10)
        self.logs.pack(fill="both", expand=True)
        ttk.Label(container, text="遇到未识别页面、低置信度答案或提交异常会停止，并保存诊断截图。", style="Subtitle.TLabel").pack(anchor="w", pady=(8, 0))
        if self.demo_url:
            self.test_button.configure(state="disabled")

    def entry(self, parent, row, label, name, **kwargs):
        ttk.Label(parent, text=label, width=12).grid(row=row, column=0, sticky="w", pady=4)
        self.fields[name] = tk.StringVar(value=getattr(self.settings, name))
        ttk.Entry(parent, textvariable=self.fields[name], **kwargs).grid(row=row, column=1, columnspan=1 if name == "browser_path" else 2, sticky="ew", pady=4)

    def check(self, parent, label, name):
        self.fields[name] = tk.BooleanVar(value=getattr(self.settings, name))
        return ttk.Checkbutton(parent, text=label, variable=self.fields[name])

    def browse(self):
        selected = filedialog.askopenfilename(title="选择浏览器可执行文件")
        if selected:
            self.fields["browser_path"].set(selected)

    def collect(self, require_model=False):
        values = {key: variable.get() for key, variable in self.fields.items()}
        values["max_chapters"] = int(values["max_chapters"])
        values["confidence_threshold"] = float(values["confidence_threshold"])
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
            self.state.set("设置已保存至项目 .runtime/settings.json")
        except (ValueError, OSError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)

    def send(self, name):
        if self.busy or self.closing:
            return
        try:
            self.settings = self.collect(require_model=name in {"start", "test"})
            self.settings.save(self.paths.runtime / "settings.json")
        except (ValueError, OSError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)
            return
        self.busy = True
        self.update_buttons()
        self.state.set({"launch": "正在打开浏览器…", "start": "开始处理课程章节…", "test": "正在测试 API…"}[name])
        self.worker.command(name, replace(self.settings))

    def stop(self):
        self.worker.stop()
        self.state.set("正在停止；正在进行的网络请求会等待返回或超时。")
        self.stop_button.configure(state="disabled")

    def update_buttons(self):
        self.launch_button.configure(state="disabled" if self.busy or self.closing else "normal")
        self.start_button.configure(state="normal" if self.ready and not self.busy and not self.closing else "disabled")
        self.stop_button.configure(state="normal" if self.busy and not self.closing else "disabled")
        self.test_button.configure(state="disabled" if self.busy or self.demo_url or self.closing else "normal")
        self.save_button.configure(state="disabled" if self.closing else "normal")

    def drain(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.logs.configure(state="normal")
                self.logs.insert("end", value + "\n")
                self.logs.see("end")
                self.logs.configure(state="disabled")
            elif kind == "state":
                self.state.set(value)
            elif kind == "url":
                parsed = value.split("?", 1)[0] if value else "尚未打开浏览器"
                self.url.set(parsed)
            elif kind == "ready":
                self.ready = value
                self.update_buttons()
            elif kind == "busy":
                self.busy = value
                self.update_buttons()
        if self.closing and not self.worker.is_alive():
            self.root.destroy()
            return
        self.root.after(100, self.drain)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.worker.stop()
        self.worker.command("shutdown")
        self.state.set("正在关闭独立浏览器并保存会话…")
        self.update_buttons()
