"""Synthetic streaming XLSX workload; report child peak RSS at several row counts.

Run with the development Python. Input, spool and JSON evidence stay in build/.
This measures the reader + Store with their runtime cache and durability settings,
not an Excel installation, a PostgreSQL load, or a whole anonymization run.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def fixture(path, count):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("_rels/.rels", f'<Relationships xmlns="{PKG}"><Relationship Id="book" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets><sheet name="Data" sheetId="1" r:id="sheet"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PKG}"><Relationship Id="sheet" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="strings" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
        with archive.open("xl/sharedStrings.xml", "w") as stream:
            stream.write(f'<sst xmlns="{MAIN}"><si><t>item</t></si>'.encode())
            for index in range(count):
                stream.write(f'<si><t>SYNTHETIC_ITEM_{index:010d}</t></si>'.encode())
            stream.write(b"</sst>")
        with archive.open("xl/worksheets/sheet1.xml", "w") as stream:
            stream.write(f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row>'.encode())
            for index in range(count):
                row = index + 2
                stream.write(f'<row r="{row}"><c r="A{row}" t="s"><v>{index + 1}</v></c></row>'.encode())
            stream.write(b"</sheetData></worksheet>")


def child(source, spool):
    # Keep all reader imports inside the measured process.
    from anonymizer.metadata import Model
    from anonymizer.store import Store
    from anonymizer import xlsx_reader
    peak_cache = 0
    original = xlsx_reader._Strings.get
    def measured_get(self, index):
        nonlocal peak_cache
        value = original(self, index)
        peak_cache = max(peak_cache, self.bytes)
        return value
    xlsx_reader._Strings.get = measured_get
    start = time.monotonic()
    with Store(spool, Model()) as store:
        xlsx_reader.parse_xlsx(source, store)
        store.flush()
        result = {"rows": store.count, "peak_string_cache_bytes": peak_cache,
                  "string_cache_limit_bytes": xlsx_reader.MAX_CACHE,
                  "spool_synchronous": store.db.execute("PRAGMA synchronous").fetchone()[0],
                  "seconds": round(time.monotonic() - start, 3)}
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, nargs="+", default=[25000, 250000, 1000000])
    parser.add_argument("--output", type=Path, default=Path("build/xlsx-memory"))
    parser.add_argument("--child", type=Path)
    parser.add_argument("--spool", type=Path)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.spool)
        return
    import psutil
    if any(n < 1 or n > 1048575 for n in args.rows):
        parser.error("row counts must fit one Excel worksheet")
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=True)
    evidence = []
    for count in args.rows:
        folder = target / str(count)
        folder.mkdir(exist_ok=True)
        source, spool = folder / "item.xlsx", folder / "spool.sqlite"
        if spool.exists():
            spool.unlink()
        fixture(source, count)
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(source), "--spool", str(spool)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        monitored = psutil.Process(process.pid)
        peak = 0
        while process.poll() is None:
            try:
                # Windows venv python.exe can be a launcher with the actual
                # interpreter in a child. Include its process tree, otherwise
                # the measurement would report only the tiny launcher.
                members = [monitored, *monitored.children(recursive=True)]
                rss, high_water = 0, 0
                for member in members:
                    try:
                        info = member.memory_info()
                        rss += info.rss
                        high_water += getattr(info, "peak_wset", info.rss)
                    except psutil.NoSuchProcess:
                        pass
                peak = max(peak, rss, high_water)
            except psutil.NoSuchProcess:
                break
            time.sleep(.025)
        stdout, stderr = process.communicate(timeout=10)
        if process.returncode:
            raise RuntimeError(f"synthetic XLSX probe failed: {stderr}")
        result = {**json.loads(stdout), "peak_rss_bytes": peak,
                  "xlsx_bytes": source.stat().st_size, "spool_bytes": spool.stat().st_size}
        assert result["rows"] == count
        assert result["peak_string_cache_bytes"] <= result["string_cache_limit_bytes"]
        evidence.append(result)
        (target / "measurements.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
