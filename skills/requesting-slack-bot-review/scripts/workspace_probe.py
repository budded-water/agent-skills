#!/usr/bin/env python3
"""Check whether validation tools can create temporary files in a checkout."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def probe(path: Path) -> dict[str, object]:
    result: dict[str, object] = {"path": str(path.resolve()), "writable": False}
    try:
        fd, probe_path = tempfile.mkstemp(prefix=".review-probe-", dir=path)
        os.close(fd)
        Path(probe_path).unlink()
        result["writable"] = True
    except OSError as error:
        result["error"] = f"{error.__class__.__name__}: {error}"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=".")
    args = parser.parse_args()
    result = probe(Path(args.path))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["writable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
