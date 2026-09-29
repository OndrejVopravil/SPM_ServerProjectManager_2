"""Odstraní z PyInstaller buildu části Qt, které aplikace nepoužívá.

QML/Quick, PDF, virtuální klávesnice, softwarové OpenGL a překlady kromě češtiny.
Zmenší rozbalenou aplikaci zhruba o polovinu.
"""

import shutil
import sys
from pathlib import Path

REMOVE = [
    "opengl32sw.dll",
    "Qt6Quick.dll",
    "Qt6Qml.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6Pdf.dll",
    "Qt6VirtualKeyboard.dll",
    "plugins/imageformats/qpdf.dll",
    "plugins/platforminputcontexts",
]


def main(dist: Path) -> None:
    qt = dist / "_internal" / "PySide6"
    for rel in REMOVE:
        path = qt / rel
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for tr in (qt / "translations").glob("*"):
        if tr.is_file() and not tr.name.startswith("qtbase_cs"):
            tr.unlink()


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist/SPM"))
