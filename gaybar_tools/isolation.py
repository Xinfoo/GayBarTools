"""Linux browser filesystem confinement, enforced before Chromium starts.

The browser sees the host filesystem read-only, with writable mounts only for
the project and /tmp. Existing GPU character devices are exposed individually
inside a private /dev; their host directories stay read-only. A private PID
namespace prevents access to another
process's host root through /proc. HOME is preserved. This protects existing
Linux NSS databases even when Chromium ignores the XDG environment variables.

Windows uses a separate project profile and explicit cache paths in browser.py;
this module does not claim Windows filesystem sandbox verification.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

from .paths import AppPaths


class IsolationError(RuntimeError):
    """Required filesystem isolation is unavailable; no browser was started."""


def _gpu_device_nodes(device_root: Path = Path("/dev")) -> list[Path]:
    """Find existing graphics devices without exposing writable host directories.

    DRM nodes cover Mesa and NVIDIA's Wayland/GBM path. The NVIDIA driver also
    uses its own character devices. Never bind a matching regular file, a
    symlink, or a path whose parents redirect outside the device tree.
    """
    inventories = (
        (device_root / "dri", r"(?:card|renderD)[0-9]+"),
        (device_root, r"nvidia(?:[0-9]+|ctl|-modeset|-uvm|-uvm-tools)"),
        (device_root / "nvidia-caps", r"nvidia-cap[0-9]+"),
    )
    devices: list[Path] = []
    for directory, pattern in inventories:
        try:
            candidates = list(directory.iterdir())
        except OSError:
            continue
        for candidate in candidates:
            if not re.fullmatch(pattern, candidate.name):
                continue
            try:
                if (stat.S_ISCHR(candidate.lstat().st_mode)
                        and candidate.resolve(strict=True) == candidate.absolute()):
                    devices.append(candidate)
            except (OSError, RuntimeError):
                # Hot-unplug, inaccessible nodes, and symlink loops must not
                # prevent a browser without hardware acceleration from starting.
                continue
    return sorted(devices)


def bubblewrap_command(executable: str, paths: AppPaths, *, bubblewrap: str | None = None) -> list[str]:
    """Build argv without shell interpolation or changing the parent environment."""
    tool = bubblewrap or shutil.which("bwrap")
    if not tool or not Path(tool).is_file():
        raise IsolationError("Linux 需要 bubblewrap（bwrap）限制浏览器写入范围；未找到该工具，浏览器未启动。")
    root = Path(paths.root).resolve()
    if root == Path("/") or not root.is_dir():
        raise IsolationError("浏览器隔离需要一个实际的项目目录，不能将 / 作为可写项目。")
    runtime = Path(paths.runtime).resolve()
    if not runtime.is_relative_to(root):
        raise IsolationError("浏览器运行目录必须位于项目目录内。")
    executable_path = Path(executable).resolve()
    if not executable_path.is_file():
        raise IsolationError(f"浏览器可执行文件不存在：{executable_path}")
    command = [str(Path(tool).resolve()), "--die-with-parent", "--new-session",
               "--unshare-user", "--unshare-pid", "--cap-drop", "ALL",
               "--ro-bind", "/", "/", "--bind", str(root), str(root),
               "--bind", "/tmp", "/tmp", "--dev", "/dev"]
    for device in _gpu_device_nodes():
        # --dev creates a minimal device tree, hiding the GPU otherwise.
        # Bind only existing character nodes: parent directories are created
        # inside the private /dev rather than made writable on the host.
        command.extend(("--dev-bind-try", str(device), str(device)))
    command.extend(("--proc", "/proc", "--chdir", str(root), "--", str(executable_path)))
    return command


def prepare_browser_executable(executable: str, paths: AppPaths) -> str:
    """Create a project-local launcher; Linux fails closed if bwrap fails.

    Playwright's protocol pipe file descriptors are inherited by the sandboxed
    child. The extra bwrap monitor/reaper processes close their copies normally.
    No restriction is ever applied to the calling Python/UI process.
    """
    if not sys.platform.startswith("linux"):
        return executable
    command = bubblewrap_command(executable, paths)
    # Probe the complete mount/PID policy before launching a browser. Never
    # silently fall back to an unrestricted executable after a failed probe.
    true_executable = shutil.which("true")
    if not true_executable:
        raise IsolationError("缺少用于检查 Linux 文件系统隔离的 true 命令，浏览器未启动。")
    try:
        result = subprocess.run(command[:-1] + [true_executable], capture_output=True,
                                text=True, timeout=10, cwd=paths.root, env=paths.environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationError(f"Linux 浏览器隔离检查失败，浏览器未启动：{exc}") from exc
    if result.returncode:
        reason = (result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}")[:700]
        raise IsolationError("Linux 浏览器隔离不可用（需要允许用户、挂载和 PID 命名空间）；"
                             f"浏览器未启动。{reason}")
    directory = (Path(paths.runtime) / "launchers").resolve()
    if not directory.is_relative_to(Path(paths.root).resolve()):
        raise IsolationError("浏览器隔离启动器目录不能通过符号链接指向项目目录之外。")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256("\0".join(command).encode()).hexdigest()[:20]
    launcher = directory / f"browser-isolated-{digest}.sh"
    content = "#!/bin/sh\nexec " + " ".join(shlex.quote(arg) for arg in command) + ' "$@"\n'
    descriptor, temporary_name = tempfile.mkstemp(prefix=".browser-launcher-", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o700)
            handle.write(content)
        temporary.replace(launcher)
    finally:
        temporary.unlink(missing_ok=True)
    return str(launcher)
