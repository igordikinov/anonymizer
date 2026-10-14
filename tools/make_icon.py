"""Builds the application icon: white In. letters on the DS dark rail.

Composites resources/ds/logo-in.png over RAIL_BACKGROUND with Tk (one-off,
needs a display), writes resources/ds/app-icon.png for the running window
and packaging/app-icon.ico for the EXE. The ico wraps plain PNG entries,
which Windows Vista+ renders directly.
"""
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from anonymizer import ds_tokens  # noqa: E402

BASE = 256
SIZES = [16, 32, 64, 128, 256]

COMPOSE = r"""
import sys
import tkinter as tk
base = int(sys.argv[1])
logo_path, out_png = sys.argv[2], sys.argv[3]
root = tk.Tk()
root.withdraw()
icon = tk.PhotoImage(master=root, width=base, height=base)
icon.put("{bg}", to=(0, 0, base, base))
logo = tk.PhotoImage(master=root, file=logo_path)
ox, oy = (base - logo.width()) // 2, (base - logo.height()) // 2
for y in range(logo.height()):
    for x in range(logo.width()):
        if not logo.transparency_get(x, y):
            r, g, b = logo.get(x, y)
            icon.put("#%02x%02x%02x" % (r, g, b), to=(ox + x, oy + y))
icon.write(out_png, format="png")
root.destroy()
""".replace("{bg}", ds_tokens.RAIL_BACKGROUND)


def main():
    logo = ROOT / "src" / "anonymizer" / "resources" / "ds" / "logo-in.png"
    icon_png = ROOT / "src" / "anonymizer" / "resources" / "ds" / "app-icon.png"
    ico_path = ROOT / "packaging" / "app-icon.ico"
    with tempfile.TemporaryDirectory(prefix="snp-icon-") as tmp:
        base_png = Path(tmp) / "base256.png"
        script = Path(tmp) / "compose.py"
        script.write_text(COMPOSE, encoding="utf-8")
        subprocess.run([sys.executable, str(script), str(BASE), str(logo), str(base_png)], check=True)
        pngs = {}
        for size in SIZES:
            factor = BASE // size
            scaled_png = Path(tmp) / f"icon{size}.png"
            scale = f"""
import tkinter as tk
root = tk.Tk(); root.withdraw()
base = tk.PhotoImage(master=root, file=r"{base_png}")
scaled = base.subsample({factor}, {factor}) if {factor} > 1 else base
scaled.write(r"{scaled_png}", format="png")
root.destroy()
"""
            scale_script = Path(tmp) / f"scale{size}.py"
            scale_script.write_text(scale, encoding="utf-8")
            subprocess.run([sys.executable, str(scale_script)], check=True)
            pngs[size] = scaled_png.read_bytes()
    icon_png.write_bytes(pngs[256])
    ordered = [(size, pngs[size]) for size in SIZES]
    offset = 6 + 16 * len(ordered)
    entries, data = bytearray(), bytearray()
    for size, png in ordered:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        data += png
    header = struct.pack("<HHH", 0, 1, len(ordered))
    ico_path.write_bytes(header + bytes(entries + data))
    print(f"icon written: {icon_png} ({icon_png.stat().st_size} bytes), {ico_path} ({ico_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
