"""Turn tools/read_plugin.py output into maps/reads.pkl.z.

usage: python make_reads.py <reads dir> <tests.json> <out.pkl.z> <sqlglot repo>

Output (pickle, zlib):
  "keys":    attribute key ("module:Class.ATTR") -> index
  "items":   index -> {item key: frozenset of test numbers that read that item}
  "all":     index -> frozenset of test numbers that read the whole container
  "collect": set of indexes read outside any test (collection time)
  "unknown": test numbers that failed under the instrumentation (their reads
             may be incomplete)
A read during setup/teardown belongs to every test in the class (setUpClass).
"""
import glob
import json
import pickle
import sys
import zlib

ALL_KEY = "<all>"

src, tests_json, dest = sys.argv[1:4]
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
groups = {}
for t in tests:
    groups.setdefault(t["id"].rsplit("::", 1)[0], set()).add(t["number"])

by_file = {}
for t in tests:
    by_file.setdefault(t["file"], set()).add(t["number"])
# test modules importing each test module (tests.dialects.test_dialect is a base for most)
import os
import re
repo = sys.argv[4] if len(sys.argv) > 4 else None
importers = {}
if repo:
    for root, _, names in os.walk(os.path.join(repo, "tests")):
        for n in names:
            if n.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, n), repo)
                text = open(os.path.join(root, n), encoding="utf-8").read()
                for m in re.finditer(r"^\s*(?:from\s+(tests[\w.]*)\s+import|import\s+(tests[\w.]*))", text, re.M):
                    mod = (m.group(1) or m.group(2)).replace(".", "/") + ".py"
                    importers.setdefault(mod, set()).add(rel)
    # transitive
    changed = True
    while changed:
        changed = False
        for mod, users in list(importers.items()):
            for u in list(users):
                extra = importers.get(u, set()) - users
                if extra:
                    users |= extra
                    changed = True

keys, items, whole, collect, unknown = {}, {}, {}, set(), set()
files = sorted(glob.glob(src + "/reads-*.pkl.z"))
for path in files:
    d = pickle.loads(zlib.decompress(open(path, "rb").read()))
    idx = []
    for names in d["labels"]:
        ids = []
        for k in names:
            ids.append(keys.setdefault(k, len(keys)))
        idx.append(ids)
    for nodeid, outcome in d["failed"].items():
        if nodeid in num:
            unknown.add(num[nodeid])
        elif nodeid.rsplit("::", 1)[0] in groups:
            unknown |= groups[nodeid.rsplit("::", 1)[0]]
    for ctx, reads in d["reads"].items():
        if ctx == "<no test>":
            for label, _ in reads:
                collect.update(idx[label])
            continue
        if ctx.startswith("<collect:"):
            # read while a test module was imported: its tests, and those of every
            # test module importing it, may depend on what was computed
            module = ctx[len("<collect:"):-1]
            owners = set(by_file.get(module, ()))
            for other in importers.get(module, ()):
                owners |= by_file.get(other, set())
            if not owners:
                for label, _ in reads:
                    collect.update(idx[label])
                continue
            for label, item in reads:
                for k in idx[label]:
                    if item == ALL_KEY:
                        whole.setdefault(k, set()).update(owners)
                    else:
                        items.setdefault(k, {}).setdefault(item, set()).update(owners)
            continue
        nodeid, phase = ctx.rsplit("|", 1)
        if phase == "call":
            owners = {num[nodeid]} if nodeid in num else set()
        else:
            owners = groups.get(nodeid.rsplit("::", 1)[0], set())
        if not owners:
            continue
        for label, item in reads:
            for k in idx[label]:
                if item == ALL_KEY:
                    whole.setdefault(k, set()).update(owners)
                else:
                    items.setdefault(k, {}).setdefault(item, set()).update(owners)

out = {
    "keys": keys,
    "items": {k: {i: frozenset(v) for i, v in m.items()} for k, m in items.items()},
    "all": {k: frozenset(v) for k, v in whole.items()},
    "collect": collect,
    "unknown": frozenset(unknown),
}
with open(dest, "wb") as f:
    f.write(zlib.compress(pickle.dumps(out, protocol=5), 9))
print("%d worker files, %d keys, %d with item reads, %d read whole, %d read at collection, "
      "%d tests unknown" % (len(files), len(keys), len(items), len(whole), len(collect), len(unknown)))
