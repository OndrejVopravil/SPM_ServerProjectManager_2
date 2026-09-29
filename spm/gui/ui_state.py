"""Uložení a obnovení stavu oken (velikost, poloha, šířky sloupců) do Settings.ui."""

from __future__ import annotations

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QHeaderView, QWidget

from ..config import Settings


def _encode(data: QByteArray) -> str:
    return bytes(data.toBase64()).decode("ascii")


def _decode(text: str) -> QByteArray:
    return QByteArray.fromBase64(text.encode("ascii"))


def save_geometry(settings: Settings, key: str, widget: QWidget) -> None:
    settings.ui[f"{key}.geometry"] = _encode(widget.saveGeometry())


def restore_geometry(settings: Settings, key: str, widget: QWidget) -> bool:
    value = settings.ui.get(f"{key}.geometry")
    return bool(value) and widget.restoreGeometry(_decode(value))


def save_header(settings: Settings, key: str, header: QHeaderView) -> None:
    settings.ui[f"{key}.header"] = _encode(header.saveState())


def restore_header(settings: Settings, key: str, header: QHeaderView) -> bool:
    value = settings.ui.get(f"{key}.header")
    return bool(value) and header.restoreState(_decode(value))
