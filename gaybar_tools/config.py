"""Validated settings, stored atomically under .runtime."""

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class AppSettings:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    request_timeout: float = 45.0
    browser_path: str = ""
    action_delay_min: float = 0.35
    action_delay_max: float = 1.0
    poll_interval: float = 2.0
    video_timeout: float = 14400.0
    max_chapters: int = 200  # Historical settings key; counts learning pages.
    headless: bool = False
    remember_key: bool = False

    def validate(self, *, require_model: bool = False) -> None:
        for name in ("base_url", "api_key", "model", "browser_path"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"配置 {name} 必须为文本。")
        parsed = urlsplit(self.base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("API 地址必须是完整的 http:// 或 https:// 地址。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API 地址不能包含账号密码、查询参数或片段。")
        if require_model and not self.model.strip():
            raise ValueError("请填写服务商提供的模型名称。")
        for name, low, high in (
            ("request_timeout", 1, 300),
            ("action_delay_min", 0, 10), ("action_delay_max", 0, 20),
            ("poll_interval", 0.05, 60), ("video_timeout", 1, 86400),
            ("max_chapters", 1, 10000),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError(f"配置 {name} 必须为数字。")
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"配置 {name} 必须在 {low}～{high} 之间。")
        if not isinstance(self.max_chapters, int):
            raise ValueError("学习页面上限必须为整数。")
        if self.action_delay_min > self.action_delay_max:
            raise ValueError("最小点击间隔不能大于最大点击间隔。")
        for name in ("headless", "remember_key"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"配置 {name} 必须为 true 或 false。")
        if self.browser_path and not Path(self.browser_path).expanduser().is_file():
            raise ValueError("指定的浏览器可执行文件不存在。")

    @classmethod
    def load(cls, path: Path) -> "AppSettings":
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("配置文件必须为 JSON 对象。")
        names = {field.name for field in fields(cls)}
        settings = cls(**{name: value for name, value in data.items() if name in names})
        settings.api_key = settings.api_key or os.environ.get("GAYBARTOOLS_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        settings.validate()
        return settings

    def save(self, path: Path) -> None:
        self.validate()
        data = asdict(self)
        if not self.remember_key:
            data["api_key"] = ""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            path.chmod(0o600)
        except OSError:
            pass
