"""Merge tools/call_plugin.py output into maps/calls.pkl.z.

usage: python make_calls.py <calls dir> <out.pkl.z>
Output: {"names": [function name], "calls": {(test, site, args hash, n): (function ids, seconds)},
         "lazy": set of function ids run while a dispatch table is built}
"""
import glob
import pickle
import sys
import zlib

src, dest = sys.argv[1:3]
names, ids, calls, lazy = [], {}, {}, set()
for path in sorted(glob.glob(src + "/calls-*.pkl.z")):
    d = pickle.loads(zlib.decompress(open(path, "rb").read()))
    remap = []
    for name in d["names"]:
        if name not in ids:
            ids[name] = len(names)
            names.append(name)
        remap.append(ids[name])
    for key, (fids, secs) in d["calls"].items():
        calls[key] = (tuple(sorted(remap[i] for i in fids)), round(secs, 5))
    lazy |= {remap[i] for i in d["lazy"]}
with open(dest, "wb") as f:
    f.write(zlib.compress(pickle.dumps({"names": names, "calls": calls, "lazy": lazy}, protocol=5), 9))
print("%d calls, %d functions, %d lazy" % (len(calls), len(names), len(lazy)))
