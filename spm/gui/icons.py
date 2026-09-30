"""Ikony souborů a složek tak, jak je zobrazuje Průzkumník Windows.

- Složky a běžné typy souborů: ikona podle přípony přes QFileIconProvider (okamžitě,
  bez přístupu k souboru – rychlé i na síťovém disku).
- Typy, jejichž ikonu určuje doplněk Průzkumníku podle obsahu souboru (v registru
  DefaultIcon = „%1“, např. díly Cimatronu .elt – díl / sestava / NC mají různé ikony):
  skutečná ikona se načítá na pozadí přes SHGetFileInfoW (asi 50 ms na soubor přes síť);
  do té doby se zobrazuje ikona podle přípony.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys

from PySide6.QtCore import QFileInfo, QThread, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QPixmap
from PySide6.QtWidgets import QFileIconProvider

_provider: QFileIconProvider | None = None
_ext_icons: dict[str, QIcon] = {}
_per_file_ext: dict[str, bool] = {}
_loader: "IconLoader | None" = None


def _prov() -> QFileIconProvider:
    global _provider
    if _provider is None:
        _provider = QFileIconProvider()
    return _provider


def folder_icon() -> QIcon:
    if "<dir>" not in _ext_icons:
        _ext_icons["<dir>"] = _prov().icon(QFileIconProvider.IconType.Folder)
    return _ext_icons["<dir>"]


def extension_icon(name: str) -> QIcon:
    """Ikona podle přípony (soubor nemusí existovat)."""
    ext = os.path.splitext(name)[1].lower()
    if ext not in _ext_icons:
        _ext_icons[ext] = _prov().icon(QFileInfo("C:/spm_ikona/soubor" + ext))
    return _ext_icons[ext]


def has_per_file_icon(name: str) -> bool:
    """Má typ souboru ikonu určenou až obsahem souboru (DefaultIcon = %1)?"""
    ext = os.path.splitext(name)[1].lower()
    if not ext or sys.platform != "win32":
        return False
    if ext not in _per_file_ext:
        _per_file_ext[ext] = _registry_per_file(ext)
    return _per_file_ext[ext]


def _registry_per_file(ext: str) -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ext) as k:
            progid = winreg.QueryValue(k, None)
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\DefaultIcon") as k:
            return "%1" in winreg.QueryValue(k, None)
    except OSError:
        return False


def shell_icons_available() -> bool:
    return sys.platform == "win32" and QGuiApplication.platformName() not in ("offscreen", "minimal")


def loader() -> "IconLoader":
    global _loader
    if _loader is None:
        _loader = IconLoader()
        _loader.start()
        app = QGuiApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(_loader.stop)
    return _loader


class IconLoader(QThread):
    """Načítá ikony jednotlivých souborů na pozadí (jedno vlákno pro celou aplikaci)."""

    loaded = Signal(str, QImage)

    def __init__(self):
        super().__init__()
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.cache: dict[str, QImage] = {}
        self.pending: set[str] = set()

    def cached(self, path: str) -> QIcon | None:
        img = self.cache.get(path)
        return QIcon(QPixmap.fromImage(img)) if img is not None and not img.isNull() else None

    def request(self, paths: list[str]) -> None:
        for p in paths:
            if p not in self.cache and p not in self.pending:
                self.pending.add(p)
                self.queue.put(p)

    def stop(self) -> None:
        # nevyřízené požadavky zahodit, ať se aplikace zavře hned
        try:
            while True:
                self.queue.get_nowait()
        except queue.Empty:
            pass
        self.queue.put(None)
        self.wait(3000)

    def run(self) -> None:
        ctypes.windll.ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED (doplňky Průzkumníku)
        while True:
            path = self.queue.get()
            if path is None:
                break
            img = _shell_icon(path)
            self.cache[path] = img
            self.pending.discard(path)
            if not img.isNull():
                self.loaded.emit(path, img)


def _shell_icon(path: str) -> QImage:
    from ctypes import wintypes

    class SHFILEINFOW(ctypes.Structure):
        _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int), ("dwAttributes", wintypes.DWORD),
                    ("szDisplayName", wintypes.WCHAR * 260), ("szTypeName", wintypes.WCHAR * 80)]

    shell32 = ctypes.windll.shell32
    shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SHFILEINFOW),
                                       wintypes.UINT, wintypes.UINT]
    shell32.SHGetFileInfoW.restype = ctypes.c_size_t
    info = SHFILEINFOW()
    SHGFI_ICON, SHGFI_SMALLICON = 0x100, 0x1
    try:
        if not shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info),
                                      SHGFI_ICON | SHGFI_SMALLICON) or not info.hIcon:
            return QImage()
        return QImage.fromHICON(info.hIcon)
    except OSError:
        return QImage()
    finally:
        if info.hIcon:
            ctypes.windll.user32.DestroyIcon(info.hIcon)
