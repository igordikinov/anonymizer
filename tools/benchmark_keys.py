"""Synthetic independent-key workload; no customer data. Runs the real three-export pipeline.

One table with two independent UNIQUE keys of high cardinality: every row carries
a unique `a` and a unique `b`, so the key group index holds ~2N distinct bodies.
This measures the GLM-002 per-group duplicate validation cost; it is not the
1 GiB free-text benchmark and not the composite-FK workload.
"""
import argparse
import json
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
        row = f"KEY{{i}}\tALT{{i}}\t{note}\n".encode()
        written = size_mb * 1024 * 1024 // len(row) // 1000 * 1000
        with src.open("wb") as stream:
            stream.write(b"CREATE TABLE public.keys_bench (a varchar(40), b varchar(40), note text,\n")
            stream.write(b"  UNIQUE (a), UNIQUE (b));\n")
            stream.write(b"COPY public.keys_bench (a,b,note) FROM stdin;\n")
            for chunk_start in range(0, written, 1000):
                stream.write(b"".join(row.replace(b"{i}", f"{i:012d}".encode()) for i in range(chunk_start, min(chunk_start + 1000, written))))
            stream.write(b"\\.\n")
        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        start = time.perf_counter()
        report = run(Options(src, root / "out", root / "project.snpanon"))
        elapsed = time.perf_counter() - start
        stop.set()
        thread.join()
        assert report["rows"] == written
        groups = {tuple(entry["columns"]): entry for entry in report["validation"]["business_key_duplicates"]}
        assert groups[("a",)]["extra_rows_before"] == groups[("a",)]["extra_rows_after"] == 0
        assert groups[("b",)]["extra_rows_before"] == groups[("b",)]["extra_rows_after"] == 0
        result = {"input_bytes": src.stat().st_size, "rows": written, "elapsed_seconds": round(elapsed, 2),
                  "peak_rss_mib": round(peak[0] / 1024**2, 2),
                  "output_bytes": sum(p.stat().st_size for p in (root / "out").rglob("*") if p.is_file()),
                  "preserved_fields_equal": report["validation"]["preserved_fields_equal"],
                  "key_groups": [{"columns": list(k), "extra_rows_before": v["extra_rows_before"],
                                  "extra_rows_after": v["extra_rows_after"]} for k, v in sorted(groups.items())],
                  "workload": f"single COPY, two independent unique varchar keys ~{written} distinct values each, "
                              f"{len(note)}-char note; full import/per-group validation/dump/inserts/csv"}
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
