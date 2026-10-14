"""Builds the application icon: white In. letters on the DS dark rail.

Composites resources/ds/logo-in.png over RAIL_BACKGROUND with Tk in-process.
The .ico follows the Win32 icon format strictly: BMP/DIB entries for
16-128px (PNG entries are only reliably supported at 256px) and a PNG entry
for 256px. resources/ds/app-icon.png (256px) feeds the running window's
iconphoto. One-off developer tool; needs a display.
"""
import struct
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from anonymizer import ds_tokens  # noqa: E402

BASE = 256
BMP_SIZES = [16, 32, 64, 128]
PNG_SIZE = 256


def dib_entry(photo, width, height):
    """Win32 DIB (BITMAPINFOHEADER + BGRA XOR bottom-up + AND mask)."""
    xor = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            r, g, b = photo.get(x, y)
            alpha = 0 if photo.transparency_get(x, y) else 255
            xor += bytes((b, g, r, alpha))
    and_stride = ((width + 31) // 32) * 4
    and_mask = bytes(and_stride * height)
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32,
                         0, len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + bytes(xor) + and_mask


def main():
    root = tk.Tk()
    root.withdraw()
    icon = tk.PhotoImage(master=root, width=BASE, height=BASE)
    icon.put(ds_tokens.RAIL, to=(0, 0, BASE, BASE))
    logo = tk.PhotoImage(master=root, file=str(ROOT / "src" / "anonymizer" / "resources" / "ds" / "logo-in.png"))
    ox, oy = (BASE - logo.width()) // 2, (BASE - logo.height()) // 2
    for y in range(logo.height()):
        for x in range(logo.width()):
            if not logo.transparency_get(x, y):
                r, g, b = logo.get(x, y)
                icon.put("#%02x%02x%02x" % (r, g, b), to=(ox + x, oy + y))

    icon_png = ROOT / "src" / "anonymizer" / "resources" / "ds" / "app-icon.png"
    png256 = tempfile_copy(icon)

    ordered = [(PNG_SIZE, png256)]
    for size in BMP_SIZES:
        scaled = icon.subsample(BASE // size, BASE // size)
        ordered.append((size, dib_entry(scaled, size, size)))
    ordered.sort(key=lambda item: item[0], reverse=True)  # крупные первыми

    ico_path = ROOT / "packaging" / "app-icon.ico"
    offset = 6 + 16 * len(ordered)
    entries, data = bytearray(), bytearray()
    for size, blob in ordered:
        width_byte = height_byte = 0 if size == PNG_SIZE else size
        entries += struct.pack("<BBBBHHII", width_byte, height_byte, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    header = struct.pack("<HHH", 0, 1, len(ordered))
    ico_path.write_bytes(header + bytes(entries + data))
    print(f"icon written: {icon_png} ({icon_png.stat().st_size} bytes), {ico_path} ({ico_path.stat().st_size} bytes)")
    root.destroy()


def tempfile_copy(photo):
    """PNG bytes of a PhotoImage without shipping the object across helpers."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        path = handle.name
    photo.write(path, format="png")
    data = Path(path).read_bytes()
    Path(path).unlink()
    return data


if __name__ == "__main__":
    main()
