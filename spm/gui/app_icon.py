"""Ikona aplikace: modrý zaoblený čtverec, bílá složka a v ní šipky nahoru / dolů
(projekty na serveru, odevzdání a stažení). Kreslí se v kódu – okno ji má bez dalších souborů;
soubor .ico pro SPM.exe z ní vyrábí tools/make_icon.py."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

SIZES = (16, 24, 32, 48, 64, 128, 256)
BLUE_TOP = QColor("#3d84d6")
BLUE_BOTTOM = QColor("#245a9e")
FOLDER = QColor("#ffffff")
FOLDER_TAB = QColor("#dce8f7")
ARROWS = QColor("#2f6fb5")


def draw(size: int) -> QImage:
    """Ikona v dané velikosti (čtvercový obrázek s průhledným okolím)."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)

    # pozadí
    margin = s * 0.03
    bg = QRectF(margin, margin, s - 2 * margin, s - 2 * margin)
    grad = QLinearGradient(bg.topLeft(), bg.bottomLeft())
    grad.setColorAt(0, BLUE_TOP)
    grad.setColorAt(1, BLUE_BOTTOM)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(bg, s * 0.2, s * 0.2)

    # složka: ouško a tělo
    left, right = s * 0.18, s * 0.82
    top, bottom = s * 0.27, s * 0.78
    tab = QPainterPath()
    tab.addRoundedRect(QRectF(left, top, s * 0.28, s * 0.14), s * 0.04, s * 0.04)
    p.setBrush(FOLDER_TAB)
    p.drawPath(tab)
    body = QRectF(left, top + s * 0.08, right - left, bottom - top - s * 0.08)
    p.setBrush(FOLDER)
    p.drawRoundedRect(body, s * 0.05, s * 0.05)

    # šipky nahoru / dolů (od 24 px – v nejmenší velikosti by splynuly)
    if size >= 24:
        pen = QPen(ARROWS, max(1.5, s * 0.055))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        cy = body.center().y()
        h = body.height() * 0.30
        head = s * 0.07
        for x, up in ((body.center().x() - s * 0.09, True), (body.center().x() + s * 0.09, False)):
            y1, y2 = (cy + h, cy - h) if up else (cy - h, cy + h)
            p.drawLine(QPointF(x, y1), QPointF(x, y2))
            d = -head if up else head
            p.drawLine(QPointF(x, y2), QPointF(x - head, y2 - d))
            p.drawLine(QPointF(x, y2), QPointF(x + head, y2 - d))
    p.end()
    return img


def app_icon() -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(draw(size)))
    return icon
