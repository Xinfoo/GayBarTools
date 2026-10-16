"""Keep application-owned files inside the project."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

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

    def activate(self) -> None:
        """Only affect this process and its children, never shell configuration."""
        self.prepare()
        os.environ.update(self.environment())
        sys.dont_write_bytecode = True
