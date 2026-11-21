"""Blast Radius selector.

For each patch, work out which tests could possibly notice the change, using a
coverage map built once from the pinned sqlglot commit (see tools/build_map.py):

* Python files in sqlglot/: compare the old and new syntax trees. Comment or
  docstring-only edits change nothing, so they select nothing. Otherwise every
  changed function selects the tests that ran any of its lines.
* Changes that take effect at import time (class attributes, module
  constants, new methods, functions that run while sqlglot is imported) also
  select every test that ran a line mentioning the changed name, and, for a
  dialect, every test that ran any of that dialect's modules. In shared core
  modules they select every test.
* Fixture files: the tests that opened that file.
* Test files: every test in that file.
* Anything we cannot reason about: every test (safe, never misses).
See app/candidates.py for the exact rules.

The selected tests are then split across up to 8 workers, trying each worker
count and keeping the cheapest (busiest worker + 2 s per worker).
"""

import pickle
import zlib
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "app"))

import candidates as C  # noqa: E402
import split  # noqa: E402

DIAG_EMPTY_ABOVE_MS = 10_000
DATA = Path(os.environ.get("DATA_DIR", "/data"))
REPO = Path(os.environ.get("SQLGLOT_DIR", "/opt/sqlglot"))


def load_maps(tests):
    raw = pickle.loads(zlib.decompress((HERE / "maps" / "coverage.pkl.z").read_bytes()))
    number_by_id = {t["id"]: t["number"] for t in tests}
    to_num = [number_by_id.get(tid) for tid in raw["test_ids"]]
    lines = raw["lines"]
    if to_num != list(range(len(to_num))):
        # tests.json is numbered differently from the map: translate
        lines = {f: {ln: frozenset(to_num[i] for i in s if to_num[i] is not None) for ln, s in m.items()}
                 for f, m in lines.items()}
    opened = {path: {number_by_id[tid] for tid in tids if tid in number_by_id}
              for path, tids in raw["opened"].items()}
    mentions = {k: {to_num[i] for i in v if to_num[i] is not None} for k, v in raw["mentions"].items()}
    return C.Maps(lines, raw["import_lines"], opened, raw["unattributed"], tests, REPO, mentions)


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    maps = load_maps(tests)
    dur_ms = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            selected, notes = C.candidates(patch.read_text(), REPO, maps)
        except Exception as error:  # never crash: fall back to running everything
            selected, notes = set(maps.all), [("error", repr(error))]
        selected = {n for n in selected if n in dur_ms}
        workers = split.best_split(selected, dur_ms)
        # DIAGNOSTIC BUILD, not an entry: commits whose selection would cost more than
        # DIAG_EMPTY_ABOVE_MS select nothing, so the board's score shows how much
        # time those commits take (simulated drop) and how many really fail (misses)
        if workers and max(sum(dur_ms[n] for n in w) for w in workers) + 2000 * len(workers) > DIAG_EMPTY_ABOVE_MS:
            workers = []
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        print(patch.stem, len(selected), notes, file=sys.stderr)


if __name__ == "__main__":
    main()
