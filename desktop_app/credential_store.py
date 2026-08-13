"""Windows user-scoped DPAPI credential storage.

The on-disk format is compatible with PowerShell's ConvertFrom-SecureString so
existing credentials continue to work. Plaintext exists only in this process.
"""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from pathlib import Path


REF_PATTERN = re.compile(r"^KCP_[A-Za-z0-9_]+$")
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob(data: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


class WindowsCredentialStore:
    def __init__(self, root: str | Path | None = None):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data and root is None:
            raise RuntimeError("LOCALAPPDATA is unavailable")
        self.root = Path(root or Path(local_app_data) / "KaggleControlPlane" / "credentials")
        self.root.mkdir(parents=True, exist_ok=True)
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    @staticmethod
    def validate_ref(credential_ref: str) -> str:
        value = credential_ref.strip().upper()
        if not REF_PATTERN.fullmatch(value):
            raise ValueError("Credential name must start with KCP_ and contain only letters, numbers, or underscores")
        return value

    def path_for(self, credential_ref: str) -> Path:
        return self.root / f"{self.validate_ref(credential_ref)}.dpapi"

    def list_refs(self) -> list[str]:
        return sorted(
            (path.stem for path in self.root.glob("KCP_*.dpapi") if REF_PATTERN.fullmatch(path.stem)),
            key=str.casefold,
        )

    def load(self, credential_ref: str) -> str:
        encrypted = bytes.fromhex(self.path_for(credential_ref).read_text(encoding="ascii").strip())
        input_blob, keepalive = _blob(encrypted)
        output_blob = DataBlob()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output_blob),
        ):
            raise ctypes.WinError()
        try:
            raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return raw.decode("utf-16-le").rstrip("\x00")
        finally:
            self._kernel32.LocalFree(output_blob.pbData)
            del keepalive

    def save(self, credential_ref: str, value: str) -> str:
        ref = self.validate_ref(credential_ref)
        if not value or not value.strip():
            raise ValueError("Token cannot be empty")
        raw = value.encode("utf-16-le")
        input_blob, keepalive = _blob(raw)
        output_blob = DataBlob()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(input_blob), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output_blob),
        ):
            raise ctypes.WinError()
        try:
            encrypted_hex = ctypes.string_at(output_blob.pbData, output_blob.cbData).hex()
            destination = self.path_for(ref)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(encrypted_hex, encoding="ascii")
            temporary.replace(destination)
        finally:
            self._kernel32.LocalFree(output_blob.pbData)
            del keepalive
        return ref

    def forget(self, credential_ref: str) -> bool:
        path = self.path_for(credential_ref)
        if not path.exists():
            return False
        path.unlink()
        return True
