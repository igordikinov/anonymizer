"""Synthetic COPY workload; no customer data. Runs the real three-export pipeline."""
import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

from anonymizer.pipeline import Options, run


def benchmark(size_mb):
    process = psutil.Process()
    peak = [process.memory_info().rss]
    stop = threading.Event()

    def sample():
        while not stop.wait(0.1):
            peak[0] = max(peak[0], process.memory_info().rss)

    with tempfile.TemporaryDirectory(prefix="snp-anon-benchmark-") as name:
        root = Path(name)
        src = root / "source.sql"
        secret = "SYNTHETIC_PRIVATE_TEXT_" * 150
        row = f"PRIVATE_ITEM\tPRIVATE_LOC\t2026-10-13\t1234567890.123456\t{secret}\n".encode()
        rows = size_mb * 1024 * 1024 // len(row)
        header = b"CREATE TABLE public.independent_demand (item varchar(40), loc varchar(20), date date, demandqty numeric(17,6), note text);\nCOPY public.independent_demand (item,loc,date,demandqty,note) FROM stdin;\n"
        with src.open("wb") as stream:
            stream.write(header)
            block = row * 1000
            for _ in range(rows // 1000):
                stream.write(block)
            stream.write(row * (rows % 1000))
            stream.write(b"\\.\n")
        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        start = time.perf_counter()
        report = run(Options(src, root / "out", root / "project.snpanon"))
        elapsed = time.perf_counter() - start
        stop.set()
        thread.join()
        assert report["rows"] == rows
        result = {"input_bytes": src.stat().st_size, "rows": rows, "elapsed_seconds": round(elapsed, 2),
                  "peak_rss_mib": round(peak[0] / 1024**2, 2), "output_bytes": sum(p.stat().st_size for p in (root / "out").rglob("*") if p.is_file()),
                  "preserved_fields_equal": report["validation"]["preserved_fields_equal"],
                  "workload": f"repeated {len(secret)}-character free text, COPY, 5 fields; full import/validation/dump/inserts/csv"}
        print(json.dumps(result, indent=2))
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=1024)
    parser.add_argument("--save", type=Path)
    args = parser.parse_args()
    result = benchmark(args.size_mb)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
