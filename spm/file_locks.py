"""Které programy mají soubory otevřené – přes Restart Manager Windows (stejné rozhraní, jakým
Průzkumník hlásí „Soubor je otevřen v programu…“). Soubory se přitom vůbec neotevírají.

Restart Manager vidí programy na tomto počítači (pro místní kopii projektu to stačí). Program,
který soubor jen načte a zavře, nenajde – ten ale ani nebrání nahrání. Vlastní proces SPM se
nepočítá. Když Restart Manager není k dispozici, použije se náhradní zkouška otevřením pro zápis
(scanner.is_open_elsewhere) – ta ale neřekne, který program soubor drží.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
ERROR_SUCCESS = 0
ERROR_MORE_DATA = 234
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _RmUniqueProcess(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class _RmProcessInfo(ctypes.Structure):
    _fields_ = [
        ("Process", _RmUniqueProcess),
        ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


class RestartManagerError(OSError):
    pass


def _api():
    rm = ctypes.WinDLL("rstrtmgr")
    rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    rm.RmStartSession.restype = wintypes.DWORD
    rm.RmRegisterResources.argtypes = [wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
                                       wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
    rm.RmRegisterResources.restype = wintypes.DWORD
    rm.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
                             ctypes.POINTER(_RmProcessInfo), ctypes.POINTER(wintypes.DWORD)]
    rm.RmGetList.restype = wintypes.DWORD
    rm.RmEndSession.argtypes = [wintypes.DWORD]
    rm.RmEndSession.restype = wintypes.DWORD
    return rm


def _exe_name(pid: int) -> str:
    """Název spustitelného souboru procesu (cimatron.exe) – prázdný, když to nejde zjistit."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        k32.CloseHandle(handle)


def _holders(rm, paths: list[Path]) -> list[tuple[int, str]]:
    """Procesy, které mají otevřený některý ze souborů: (pid, název programu)."""
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    err = rm.RmStartSession(ctypes.byref(session), 0, key)
    if err != ERROR_SUCCESS:
        raise RestartManagerError(f"RmStartSession: {err}")
    try:
        names = (wintypes.LPCWSTR * len(paths))(*[str(p) for p in paths])
        err = rm.RmRegisterResources(session, len(paths), names, 0, None, 0, None)
        if err != ERROR_SUCCESS:
            raise RestartManagerError(f"RmRegisterResources: {err}")
        needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD(0)
        infos = None
        for _ in range(5):  # seznam se mezi dotazy může zvětšit
            count = wintypes.UINT(needed.value)
            infos = (_RmProcessInfo * max(1, needed.value))()
            err = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
            if err != ERROR_MORE_DATA:
                break
        if err != ERROR_SUCCESS:
            raise RestartManagerError(f"RmGetList: {err}")
        own = os.getpid()
        return [(infos[i].Process.dwProcessId, infos[i].strAppName) for i in range(count.value)
                if infos[i].Process.dwProcessId != own]
    finally:
        rm.RmEndSession(session)


def _label(pid: int, app: str) -> str:
    exe = _exe_name(pid)
    if app and exe and app.lower() != exe.lower():
        return f"{app} ({exe})"
    return app or exe or f"proces {pid}"


def open_by(paths: list[Path]) -> dict[Path, list[str]]:
    """Soubory, které má otevřené jiný program na tomto počítači -> programy (např.
    „Cimatron (cimatron.exe)“). Neexistující soubory se přeskočí."""
    paths = [p for p in paths if p.is_file()]
    if not paths:
        return {}
    if sys.platform != "win32":
        return {}
    try:
        rm = _api()
        if not _holders(rm, paths):  # jeden dotaz na všechny – nejčastěji nic
            return {}
        result: dict[Path, list[str]] = {}
        for p in paths:  # někdo je drží: zjistit, které soubory a kdo
            holders = _holders(rm, [p])
            if holders:
                result[p] = sorted({_label(pid, app) for pid, app in holders}, key=str.lower)
        return result
    except (OSError, AttributeError):
        # Restart Manager nedostupný – náhradní zkouška otevřením pro zápis (bez názvu programu)
        from .scanner import is_open_elsewhere
        return {p: [] for p in paths if is_open_elsewhere(p)}
