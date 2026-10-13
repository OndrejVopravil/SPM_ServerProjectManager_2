"""Identita aktuálního uživatele a počítače."""

import ctypes
import getpass
import socket
import sys


def current_user() -> str:
    return getpass.getuser()


def current_pc() -> str:
    return socket.gethostname()


def current_full_name() -> str:
    """Celé jméno uživatele z Windows (např. „Ondřej Vopravil“); "" pokud ho systém nezná."""
    if sys.platform != "win32":
        return ""
    try:
        name_display = 3  # EXTENDED_NAME_FORMAT.NameDisplay
        size = ctypes.c_ulong(0)
        secur32 = ctypes.windll.secur32
        secur32.GetUserNameExW(name_display, None, ctypes.byref(size))
        if not size.value:
            return ""
        buf = ctypes.create_unicode_buffer(size.value)
        if secur32.GetUserNameExW(name_display, buf, ctypes.byref(size)):
            return buf.value.strip()
    except (OSError, AttributeError):
        pass
    return ""
