"""Private, project-local snapshots of browser cookies, including sessions.

Snapshot data is only decoded as JSON and passed to Playwright's cookie API.
No cookie identifiers or values belong in exceptions or application logs.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any

from .paths import AppPaths


class CookieStoreError(RuntimeError):
    """A fixed, non-sensitive description of a snapshot failure."""


_UNSAFE_PATH = "登录状态文件路径不安全，已跳过读取或保存。"
_INVALID_FILE = "本地登录状态文件损坏或格式不支持，已跳过恢复。"
_READ_ERROR = "无法读取项目内的登录状态文件，已跳过恢复。"
_WRITE_ERROR = "无法保存项目内的登录状态文件，请检查项目目录权限和可用空间。"
_MAX_BYTES = 8 * 1024 * 1024
# Playwright rejects larger dates before handing a batch to Chromium.
_MAX_EXPIRY = 253402300799
_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_DOMAIN = re.compile(r"^\.?(?:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*|\[[0-9A-Fa-f:]+\])$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def valid_cookies(cookies: list[Any], *, now: float | None = None) -> list[dict[str, Any]]:
    """Copy only supported, well-formed, unexpired Playwright cookie fields."""
    current = time.time() if now is None else now
    result: list[dict[str, Any]] = []
    for raw in cookies:
        if not isinstance(raw, dict):
            continue
        name, value, domain, path = (raw.get(key) for key in ("name", "value", "domain", "path"))
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or not isinstance(value, str) or _CONTROL.search(value)
                or not isinstance(domain, str) or not _DOMAIN.fullmatch(domain)
                or not isinstance(path, str) or not path.startswith("/") or _CONTROL.search(path)):
            continue
        cookie: dict[str, Any] = {"name": name, "value": value, "domain": domain, "path": path}
        expiry = raw.get("expires", -1)
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            continue
        try:
            if (not math.isfinite(expiry) or expiry > _MAX_EXPIRY
                    or (expiry != -1 and expiry <= current)):
                continue
        except OverflowError:
            continue
        cookie["expires"] = expiry
        if any(key in raw and type(raw[key]) is not bool for key in ("httpOnly", "secure")):
            continue
        for key in ("httpOnly", "secure"):
            if key in raw:
                cookie[key] = raw[key]
        if "sameSite" in raw:
            if raw["sameSite"] not in ("Strict", "Lax", "None"):
                continue
            cookie["sameSite"] = raw["sameSite"]
        if raw.get("partitionKey") is not None:
            key = raw["partitionKey"]
            if not isinstance(key, str) or not key or _CONTROL.search(key):
                continue
            cookie["partitionKey"] = key
            # Chromium's Playwright driver returns this optional boolean in
            # addition to partitionKey. Without it a first-party partition
            # would be restored as a different, cross-site partition.
            if "_crHasCrossSiteAncestor" in raw:
                if type(raw["_crHasCrossSiteAncestor"]) is not bool:
                    continue
                cookie["_crHasCrossSiteAncestor"] = raw["_crHasCrossSiteAncestor"]
        result.append(cookie)
    # Chromium can return the same jar in a different order on later reads.
    return sorted(result, key=lambda item: (item["domain"], item["path"], item["name"],
                                             item.get("partitionKey", "")))


class CookieStore:
    """One snapshot per isolated profile; normal and demo profiles stay apart."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._last_payload: bytes | None = None

    def _directory(self, *, create: bool) -> Path:
        try:
            root = Path(self.paths.root).resolve()
            runtime = Path(self.paths.runtime).resolve()
            profile = Path(self.paths.profile).resolve()
            if (runtime == root or not runtime.is_relative_to(root)
                    or profile == runtime or not profile.is_relative_to(runtime)):
                raise CookieStoreError(_UNSAFE_PATH)
            # Validate before mkdir, so even an absent directory cannot create
            # files through a profile/runtime symlink outside the project.
            if create:
                profile.mkdir(parents=True, exist_ok=True, mode=0o700)
                if Path(self.paths.profile).resolve() != profile:
                    raise CookieStoreError(_UNSAFE_PATH)
            return profile
        except (OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, CookieStoreError):
                raise
            raise CookieStoreError(_UNSAFE_PATH) from None

    @staticmethod
    def _check_file(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CookieStoreError(_UNSAFE_PATH)

    @staticmethod
    def _encode(cookies: list[dict[str, Any]]) -> bytes:
        return (json.dumps({"version": 1, "cookies": cookies}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

    def load(self) -> list[dict[str, Any]] | None:
        """Return None when absent; an empty list is a deliberate empty jar."""
        descriptor: int | None = None
        try:
            path = self._directory(create=False) / "cookies.json"
            self._check_file(path)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CookieStoreError(_UNSAFE_PATH)
            if info.st_size > _MAX_BYTES:
                raise CookieStoreError(_INVALID_FILE)
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                payload = source.read(_MAX_BYTES + 1)
                if len(payload) > _MAX_BYTES:
                    raise CookieStoreError(_INVALID_FILE)
                # Repair permissions of files produced by an earlier version.
                if os.name != "nt":
                    os.fchmod(source.fileno(), 0o600)
            data = json.loads(payload)
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != 1 or not isinstance(data.get("cookies"), list)):
                raise CookieStoreError(_INVALID_FILE)
            cookies = valid_cookies(data["cookies"])
            self._last_payload = self._encode(cookies)
            return cookies
        except FileNotFoundError:
            return None
        except CookieStoreError:
            raise
        except (UnicodeError, ValueError, RecursionError):
            raise CookieStoreError(_INVALID_FILE) from None
        except OSError:
            raise CookieStoreError(_READ_ERROR) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def save(self, cookies: list[dict[str, Any]]) -> bool:
        """Atomically replace the jar; never merge cookies from older saves."""
        temporary: str | None = None
        descriptor: int | None = None
        try:
            directory = self._directory(create=True)
            path = directory / "cookies.json"
            self._check_file(path)
            payload = self._encode(valid_cookies(cookies))
            if len(payload) > _MAX_BYTES:
                raise CookieStoreError(_WRITE_ERROR)
            if payload == self._last_payload and path.is_file():
                if os.name != "nt":
                    path.chmod(0o600)
                return False
            descriptor, temporary = tempfile.mkstemp(prefix=".cookies-", suffix=".tmp", dir=directory)
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = None
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            if self._directory(create=False) != directory:
                raise CookieStoreError(_UNSAFE_PATH)
            self._check_file(path)
            os.replace(temporary, path)
            temporary = None
            self._last_payload = payload
            return True
        except CookieStoreError:
            raise
        except (OSError, TypeError, ValueError, RecursionError):
            raise CookieStoreError(_WRITE_ERROR) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
