"""Regenerate the supplied brand icon with the isolated developer toolchain.

Run: build/brand-tools/Scripts/python.exe tools/make_icon.py
The canonical ICO is copied unchanged; the window PNG uses its largest frame.
"""
import sys

from prepare_brand import main


if __name__ == "__main__":
    sys.argv.append("--icons-only")
    main()
