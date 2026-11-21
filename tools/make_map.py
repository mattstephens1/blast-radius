"""Turn the coverage run (tools/coverage_plugin.py) into maps/coverage.json.gz.

usage: python make_map.py <map_out_dir> <sqlglot_root> <tests.json> <output.json.gz>

* A line run during a test's call phase belongs to that test.
* A line run during setup/teardown belongs to every test in the same class
  (setUpClass runs once, inside whichever test comes first).
* A line run while no test was active ran at import time.
"""
import gzip
import json
import sys

import coverage

out_dir, root, tests_json, dest = sys.argv[1:5]
root = root.rstrip("/") + "/"
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]
idx = {tid: i for i, tid in enumerate(ids)}
groups = {}
for tid, i in idx.items():
    groups.setdefault(tid.rsplit("::", 1)[0], set()).add(i)

data = coverage.CoverageData(f"{out_dir}/.cov")
data.read()
lines, import_lines = {}, {}
for f in data.measured_files():
    rel = f[len(root):]
    m, imp = {}, []
    for ln, contexts in data.contexts_by_lineno(f).items():
        s = set()
        for c in contexts:
            if not c:
                imp.append(ln)
                continue
            nodeid, phase = c.rsplit("|", 1)
            if nodeid not in idx:
                continue
            if phase == "call":
                s.add(idx[nodeid])
            else:
                s |= groups[nodeid.rsplit("::", 1)[0]]
        if s:
            m[ln] = sorted(s)
    lines[rel] = m
    if imp:
        import_lines[rel] = sorted(imp)

# keys are "<nodeid>|<phase>"; a file read during setup (setUpClass) is used by
# the whole class, not just the test that happened to run first
raw_opens = json.load(open(f"{out_dir}/opened.json"))
unattributed = raw_opens.pop("<no test>", [])
opened, opened_raw = {}, {}
for key, files in raw_opens.items():
    tid, phase = key.rsplit("|", 1)
    if tid not in idx:
        continue
    owners = {tid} if phase == "call" else {ids[i] for i in groups[tid.rsplit("::", 1)[0]]}
    for path in files:
        opened.setdefault(path, set()).update(owners)
        for o in owners:
            opened_raw.setdefault(o, set()).add(path)

# Which tests mention each dialect by name: in their own source (e.g. read="tsql"),
# in a fixture they read ("# dialect: tsql"), or by living in tests/dialects/test_<name>.py.
import ast
import os
import re
dialects = sorted(p[:-3] for p in os.listdir(root + "sqlglot/dialects")
                  if p.endswith(".py") and p not in ("__init__.py", "dialect.py"))
func_src = {}
for path in sorted({t["file"] for t in tests}):
    text = open(root + path).read()
    tree = ast.parse(text)
    for cls in tree.body:
        members = cls.body if isinstance(cls, ast.ClassDef) else [cls]
        for fn in members:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = f"{path}::{cls.name}::" if isinstance(cls, ast.ClassDef) else f"{path}::"
                func_src[prefix + fn.name] = ast.get_source_segment(text, fn) or ""
# which dialects each fixture file names (compressed data files are skipped)
fixture_dialects = {}
pats = {name: re.compile(r"[\"']" + re.escape(name) + r"[\"']|dialect:\s*" + re.escape(name) + r"\b", re.I)
        for name in dialects}
for path in opened:
    if path.endswith((".py", ".pyc", ".gz")):
        continue
    try:
        text = open(root + path, errors="replace").read()
    except OSError:
        continue
    fixture_dialects[path] = {n for n, p in pats.items() if p.search(text)}
mentions = {name: set() for name in dialects}
for tid, i in idx.items():
    src = func_src.get(tid, "")
    for name, p in pats.items():
        if tid.startswith(f"tests/dialects/test_{name}.py::") or p.search(src):
            mentions[name].add(i)
    for path in opened_raw.get(tid, ()):
        for name in fixture_dialects.get(path, ()):
            mentions[name].add(i)
mentions = {k: sorted(v) for k, v in mentions.items()}

with gzip.open(dest, "wt") as fh:
    json.dump({"test_ids": ids, "lines": lines, "import_lines": import_lines,
               "opened": {k: sorted(v) for k, v in opened.items()},
               "unattributed": [u for u in unattributed if not u.endswith((".py", ".pyc"))],
               "mentions": mentions}, fh, separators=(",", ":"))
print(len(lines), "files;", len(opened), "fixtures;", len(unattributed), "files opened outside tests")
