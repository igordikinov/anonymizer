"""Synthetic composite-FK workload; no customer data. Runs the real three-export pipeline.

High-cardinality two-column keys: every parent row carries a unique tuple and every
child row references a valid parent tuple, so the tuple index holds ~2N distinct keys.
This measures the GLM-001 tuple validation cost; it is not the 1 GiB free-text benchmark.
"""
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
        note = "SYNTHETIC_NOTE_" * 20
        row = f"KEY{{item}}\tLOC{{loc}}\t{note}\n".encode()
        rows = size_mb * 1024 * 1024 // (2 * len(row))
        with src.open("wb") as stream:
            written = rows // 1000 * 1000
            stream.write(b"CREATE TABLE public.parent_bench (item varchar(40), loc varchar(20), note text);\n")
            stream.write(b"CREATE TABLE public.child_bench (item varchar(40), loc varchar(20), qty numeric(17,6), note text,\n")
            stream.write(b"  FOREIGN KEY (item,loc) REFERENCES public.parent_bench(item,loc));\n")
            stream.write(b"COPY public.parent_bench (item,loc,note) FROM stdin;\n")
            block = b"".join(row.replace(b"{item}", f"{i:012d}".encode()).replace(b"{loc}", f"L{i%97}".encode()) for i in range(1000))
            for _ in range(written // 1000):
                stream.write(block)
            stream.write(b"\\.\n")
            stream.write(b"COPY public.child_bench (item,loc,qty,note) FROM stdin;\n")
            row2 = f"KEY{{item}}\tLOC{{loc}}\t1234567890.123456\t{note}\n".encode()
            block2 = b"".join(row2.replace(b"{item}", f"{i:012d}".encode()).replace(b"{loc}", f"L{i%97}".encode()) for i in range(1000))
            for _ in range(written // 1000):
                stream.write(block2)
            stream.write(b"\\.\n")
        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        start = time.perf_counter()
        report = run(Options(src, root / "out", root / "project.snpanon"))
        elapsed = time.perf_counter() - start
        stop.set()
        thread.join()
        assert report["rows"] == 2 * written
        (fk,) = report["validation"]["foreign_keys"]
        assert fk["columns"] == ["item", "loc"] and fk["missing_before"] == fk["missing_after"] == 0
        result = {"input_bytes": src.stat().st_size, "rows": 2 * written, "elapsed_seconds": round(elapsed, 2),
                  "peak_rss_mib": round(peak[0] / 1024**2, 2),
                  "output_bytes": sum(p.stat().st_size for p in (root / "out").rglob("*") if p.is_file()),
                  "preserved_fields_equal": report["validation"]["preserved_fields_equal"],
                  "composite_fk": {"columns": fk["columns"], "missing_before": fk["missing_before"], "missing_after": fk["missing_after"]},
                  "workload": f"parent+child COPY, unique 2-column keys (item,loc) ~{rows} tuples per side, {len(note)}-char note; "
                              f"full import/tuple validation/dump/inserts/csv"}
        print(json.dumps(result, indent=2))
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=512)
    parser.add_argument("--save", type=Path)
    args = parser.parse_args()
    result = benchmark(args.size_mb)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
