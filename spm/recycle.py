"""Přesun souborů do Koše Windows (ne trvalé smazání) – přes SHFileOperationW."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040        # = do Koše
FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def to_recycle_bin(paths: list[Path]) -> None:
    """Přesune soubory / složky do Koše jednou operací. Při chybě vyhodí OSError."""
    if not paths:
        return
    if sys.platform != "win32":
        raise OSError("Koš je podporován jen ve Windows.")
    # seznam cest oddělených \0 a zakončený dvojitým \0; cesty musí být úplné
    buffer = "\0".join(str(Path(p).resolve()) for p in paths) + "\0\0"
    op = _SHFILEOPSTRUCTW(
        hwnd=None, wFunc=FO_DELETE, pFrom=buffer, pTo=None,
        fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        raise OSError(f"Přesun do Koše se nezdařil (kód {result:#x}). Není některý soubor otevřený?")
    still = [str(p) for p in paths if Path(p).exists()]
    if still:
        raise OSError("Tyto položky se nepodařilo přesunout do Koše:\n" + "\n".join(still[:15]))
