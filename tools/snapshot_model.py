"""Build metadata-only snapshot from the user's local YAML model; no examples."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import yaml  # development-time dependency only


def build(source, target):
    tables, digest = {}, hashlib.sha256()
    for path in sorted((source / "model/tables").glob("*.yaml")):
        data = path.read_bytes()
        digest.update(path.name.encode() + data)
        table = yaml.safe_load(data)
        tables[table["code"]] = {"fields": [
            {key: field.get(key) for key in ("name", "type", "size", "keys", "nullable", "fk")}
            for field in table["fields"]
        ]}
    if not tables:
        raise ValueError("Model contains no tables")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    result = {"meta": {"version": (source / "VERSION").read_text().strip(), "git_sha": sha,
                        "content_sha256": digest.hexdigest(), "table_count": len(tables)}, "tables": tables}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Snapshot: {len(tables)} tables, model {result['meta']['version']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    build(args.source, args.target)
