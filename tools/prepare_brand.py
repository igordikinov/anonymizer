"""Prepare the provided In.Plan brand assets and static GDI fonts offline.

Developer-only tool: use build/brand-tools/Scripts/python.exe with the pinned
requirements-brand.txt. None of these conversion libraries is a runtime import.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "resources/brand/frontend-brand.tgz"
SOURCE_SHA256 = "904780db9b8a39671939560e013793e043eb76de4be68e640803f282c8af0ce5"
SHELL = "frontend-brand/files/projects/scp-shell/src/"
SHARED = "frontend-brand/files/projects/scp-shared/src/lib/common/utils/brand-palette.ts"
FONT_WEIGHTS = {400: "Regular", 600: "SemiBold", 700: "Bold"}
GDI_FAMILY = "InPlan Desktop Manrope"
REQUIRED_TEXT = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
                 "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
                 "№—–…•«».,:;!?()/+-=_@% ")


def fingerprint(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"name": path.name, **fingerprint(data)}


def read_part(archive, name):
    # Read only explicitly named regular files; never execute/extract AGENT.md.
    member = archive.getmember(name)
    if not member.isfile() or member.size > 8 * 1024**2:
        raise ValueError("Unexpected brand resource")
    return archive.extractfile(member).read()


def prepare_assets(archive):
    from PIL import Image
    dest = ROOT / "src/anonymizer/resources/ds"
    entries = []
    for relative, name in [(SHELL + "app/styles/brand.scss", "brand.scss"),
                           (SHARED, "brand-palette.ts"),
                           (SHELL + "assets/images/header-logo-in.png", "header-logo-in.png")]:
        entries.append({**save(dest / name, read_part(archive, relative)), "archive_member": relative})
    ico_member = SHELL + "favicon.ico"
    ico = read_part(archive, ico_member)
    entries.append({**save(ROOT / "packaging/app-icon.ico", ico), "archive_member": ico_member})
    icon = Image.open(io.BytesIO(ico))
    largest = max(icon.ico.sizes(), key=lambda size: size[0] * size[1])
    icon = icon.ico.getimage(largest).convert("RGBA")
    if icon.getpixel((0, 0)) != (4, 20, 58, 255):
        raise ValueError("Brand favicon background changed")
    buffer = io.BytesIO()
    icon.resize((256, 256), Image.Resampling.LANCZOS).save(buffer, format="PNG")
    entries.append({**save(dest / "app-icon.png", buffer.getvalue()), "archive_member": ico_member,
                    "conversion": f"largest {largest[0]}px ICO frame to 256px PNG; Pillow LANCZOS"})
    manifest = {"source_archive": "resources/brand/frontend-brand.tgz", "archive_sha256": SOURCE_SHA256,
                "tools": {"pillow": version("pillow")}, "files": entries,
                "desktop_adaptations": {"primary_hover": "#3d5ce1", "primary_pressed": "#3350c4",
                    "hover_dark_mix": .15, "pressed_dark_mix": .30,
                    "light_surface_secondary_text": "#04143a", "field_px": 12}}
    save(dest / "brand-provenance.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())


def _names(font, weight, style):
    # The supplied static instances still call themselves "Manrope ExtraLight".
    # Keep outlines/weight, normalize only naming/style metadata for private GDI.
    family = GDI_FAMILY + " SemiBold" if weight == 600 else GDI_FAMILY
    subfamily = "Bold" if weight == 700 else "Regular"
    records = {1: family, 2: subfamily, 3: f"InPlan-Manrope-{style}-{SOURCE_SHA256[:12]}",
               4: GDI_FAMILY + " " + style, 6: "InPlanDesktopManrope-" + style,
               16: GDI_FAMILY, 17: style}
    table = font["name"]
    for ident, value in records.items():
        table.removeNames(nameID=ident)
        table.setName(value, ident, 3, 1, 0x409)
        table.setName(value, ident, 1, 0, 0)
    font["OS/2"].usWeightClass = weight
    font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~((1 << 5) | (1 << 6))) | (1 << (5 if weight == 700 else 6))
    font["head"].macStyle = (font["head"].macStyle & ~1) | (1 if weight == 700 else 0)


def prepare_fonts(archive):
    from fontTools.merge import Merger
    from fontTools.ttLib import TTFont
    dest = ROOT / "src/anonymizer/resources/fonts"
    inputs, outputs = [], []
    with tempfile.TemporaryDirectory(prefix="snp-brand-fonts-") as name:
        directory = Path(name)
        for weight, style in FONT_WEIGHTS.items():
            files = []
            expected_cmap = set()
            source_timestamps = None
            for subset in ("latin", "cyrillic"):
                member = SHELL + f"assets/fonts/manrope/manrope-{subset}-{weight}-normal.woff2"
                data = read_part(archive, member)
                inputs.append({"archive_member": member, **fingerprint(data)})
                font = TTFont(io.BytesIO(data), recalcTimestamp=False)
                if source_timestamps is None:
                    source_timestamps = font["head"].created, font["head"].modified
                if "fvar" in font or font["OS/2"].usWeightClass != weight:
                    raise ValueError("Expected static Manrope instance")
                expected_cmap.update(font.getBestCmap())
                font.flavor = None
                path = directory / f"{weight}-{subset}.ttf"
                font.save(path)
                font.close()
                files.append(str(path))
            font = Merger().merge(files)
            font.flavor = None
            font.recalcTimestamp = False
            # Merger initializes a fresh head with the current clock even when
            # its inputs disable timestamp recalculation. Restore source dates.
            font["head"].created, font["head"].modified = source_timestamps
            _names(font, weight, style)
            cmap = font.getBestCmap()
            if set(cmap) != expected_cmap or not all(ord(char) in cmap for char in REQUIRED_TEXT):
                raise ValueError("Merged Manrope lost required glyphs")
            path = directory / f"Manrope-{style}.ttf"
            font.save(path)
            font.close()
            data = path.read_bytes()
            outputs.append({**save(dest / path.name, data), "weight": weight, "unicode_count": len(cmap),
                            "gdi_family": GDI_FAMILY + " SemiBold" if weight == 600 else GDI_FAMILY})
    license_member = SHELL + "assets/fonts/manrope/OFL.txt"
    outputs.append({**save(dest / "Manrope-OFL.txt", read_part(archive, license_member)),
                    "archive_member": license_member})
    manifest = {"source_archive": "resources/brand/frontend-brand.tgz", "archive_sha256": SOURCE_SHA256,
                "license": "SIL Open Font License 1.1", "source_project": "https://github.com/sharanda/manrope",
                "tools": {key: version(key) for key in ("fonttools", "brotli", "zopfli")},
                "recipe": "decode latin then cyrillic static subsets; FontTools Merger; uncompressed TTF; normalized GDI names; timestamps from source",
                "required_characters": REQUIRED_TEXT, "inputs": inputs, "files": outputs}
    save(dest / "manrope-provenance.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icons-only", action="store_true")
    args = parser.parse_args()
    data = SOURCE.read_bytes()
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise SystemExit("Unexpected frontend-brand.tgz SHA256")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        prepare_assets(archive)
        if not args.icons_only:
            prepare_fonts(archive)
    print("Brand resources prepared from", SOURCE_SHA256)


if __name__ == "__main__":
    main()
