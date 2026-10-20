"""Keep application-owned files inside the project."""

import os
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from xml.etree import ElementTree

_SOURCE_ROOT = Path(__file__).resolve().parent.parent
if (_SOURCE_ROOT / "pyproject.toml").is_file():
    PROJECT_ROOT = _SOURCE_ROOT
elif sys.prefix != sys.base_prefix and Path(sys.prefix).name == ".venv":
    PROJECT_ROOT = Path(sys.prefix).resolve().parent
else:
    # A wheel installed elsewhere uses the launch directory as its project;
    # application data must never be written into the system site-packages.
    PROJECT_ROOT = Path.cwd().resolve()


@dataclass(frozen=True)
class AppPaths:
    root: Path
    runtime: Path
    profile: Path
    logs: Path
    screenshots: Path
    browsers: Path
    cache: Path
    tmp: Path

    @classmethod
    def for_root(cls, root: Path = PROJECT_ROOT) -> "AppPaths":
        root = Path(root).resolve()
        runtime = root / ".runtime"
        return cls(root, runtime, runtime / "browser-profile", runtime / "logs",
                   runtime / "screenshots", runtime / "browsers", runtime / "cache",
                   runtime / "tmp")

    def prepare(self) -> "AppPaths":
        if not self.runtime.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("运行目录不能通过软链接指向项目目录之外。")
        for directory in (self.profile, self.logs, self.screenshots, self.cache, self.tmp):
            if not directory.resolve().is_relative_to(self.runtime.resolve()):
                raise ValueError(f"运行数据目录不能通过软链接指向项目之外：{directory.name}")
        for directory in (self.runtime, self.profile, self.logs, self.screenshots,
                          self.browsers, self.cache, self.tmp):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.runtime.chmod(0o700)
        except OSError:
            pass  # Windows ACLs follow the containing project directory.
        return self

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if sys.platform.startswith("linux"):
            self._preserve_user_fonts()
        env.update({
            "TMPDIR": str(self.tmp), "TMP": str(self.tmp), "TEMP": str(self.tmp),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_CONFIG_HOME": str(self.runtime / "config"),
            "XDG_DATA_HOME": str(self.runtime / "data"),
            "XDG_STATE_HOME": str(self.runtime / "state"),
            "PLAYWRIGHT_BROWSERS_PATH": str(self.browsers),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_CACHE_DIR": str(self.runtime / "pip-cache"),
        })
        return env

    @cached_property
    def _font_sources(self) -> tuple[Path, Path]:
        """Remember the source before activate() changes this process's XDG homes."""
        home = Path.home()
        source_config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        source_data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
        # XDG paths must be absolute; invalid relative values use the defaults.
        if not source_config.is_absolute():
            source_config = home / ".config"
        if not source_data.is_absolute():
            source_data = home / ".local/share"
        return source_config, source_data

    def _preserve_user_fonts(self) -> None:
        """Keep host fonts readable when XDG homes move into the project."""
        source_config, source_data = self._font_sources
        config = self.runtime / "config"
        if source_config.resolve() == config.resolve():
            # activate() already installed this bridge. Do not replace its
            # original sources with the redirected paths or include itself.
            return
        bridge = config / "fontconfig/conf.d/00-gaybar-host-fonts.conf"
        if (not self.runtime.resolve().is_relative_to(self.root.resolve())
                or not bridge.resolve().is_relative_to(self.runtime.resolve())):
            raise ValueError("字体配置不能通过软链接写入项目目录之外。")
        document = ElementTree.Element("fontconfig")
        # Prefer our writable cache even if a user rule declares a cache path.
        ElementTree.SubElement(document, "cachedir").text = str(self.cache / "fontconfig")
        for suffix in ("conf.d", "fonts.conf"):
            ElementTree.SubElement(document, "include", ignore_missing="yes").text = str(
                source_config / "fontconfig" / suffix)
        # A user rule may reset the directory list; restore user fonts last.
        ElementTree.SubElement(document, "dir").text = str(source_data / "fonts")
        content = '<?xml version="1.0"?>\n' + ElementTree.tostring(document, encoding="unicode") + "\n"
        bridge.parent.mkdir(parents=True, exist_ok=True)
        if not bridge.is_file() or bridge.read_text(encoding="utf-8") != content:
            bridge.write_text(content, encoding="utf-8")

    def activate(self) -> None:
        """Only affect this process and its children, never shell configuration."""
        self.prepare()
        os.environ.update(self.environment())
        sys.dont_write_bytecode = True
