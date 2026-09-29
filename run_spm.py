"""Vstupní bod pro PyInstaller (balíček spm používá relativní importy)."""

import sys

from spm.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
