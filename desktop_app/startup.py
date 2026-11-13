"""Per-user Windows sign-in startup; no administrator rights or secrets."""
from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "KaggleControlPlane"


def _executable(executable: Path | None) -> Path | None:
    if executable is not None:
        return Path(executable).resolve()
    return Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None


def get_startup(executable: Path | None = None) -> dict:
    target = _executable(executable)
    if sys.platform != "win32" or target is None:
        return {"supported": False, "enabled": False, "registered": False,
                "reason": "Available in the installed Windows application."}
    import winreg
    expected = f'"{target}"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        command = None
    disabled = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            disabled = isinstance(value, bytes) and bool(value) and value[0] in (3, 7)
    except FileNotFoundError:
        pass
    registered = command == expected
    return {"supported": True, "enabled": registered and not disabled,
            "registered": registered, "disabled_by_windows": disabled}


def set_startup(enabled: bool, executable: Path | None = None) -> dict:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    target = _executable(executable)
    if sys.platform != "win32" or target is None:
        raise ValueError("Startup requires the installed Windows application.")
    if enabled and (not target.is_file() or target.suffix.lower() != ".exe"):
        raise ValueError("The installed application executable was not found.")
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, f'"{target}"')
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    # An explicit user toggle also clears Windows Task Manager's disable flag
    # for this app only. Other startup entries are never modified.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
    state = get_startup(target)
    if state["enabled"] != enabled:
        raise OSError("Windows startup setting could not be verified.")
    return state


class SingleInstance:
    """Hold a session-local named mutex for the lifetime of one desktop app."""
    def __init__(self):
        self._handle = None
        self._kernel = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        scope = os.path.normcase(os.environ.get("LOCALAPPDATA", str(Path.home())))
        name = "Local\\KaggleControlPlane-" + hashlib.sha256(scope.encode()).hexdigest()[:20]
        handle = kernel.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel.CloseHandle(handle)
            return False
        self._kernel, self._handle = kernel, handle
        return True

    def close(self) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
