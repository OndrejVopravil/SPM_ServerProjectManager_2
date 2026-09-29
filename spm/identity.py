"""Identita aktuálního uživatele a počítače."""

import getpass
import socket


def current_user() -> str:
    return getpass.getuser()


def current_pc() -> str:
    return socket.gethostname()
