"""usage: misses.py <labels.txt> <selector out.jsonl> : real failures vs selection"""
import json, collections, sys
t = {x["id"]: x["number"] for x in json.load(open("sample/tests.json"))["tests"]}
fail, done = collections.defaultdict(set), []
for l in open(sys.argv[1]):
    p = l.split(maxsplit=1)
    if len(p) < 2: continue
    i, n = p[0], p[1].strip()
    if n == "DONE": done.append(i)
    elif n != "PATCHFAIL" and n in t: fail[i].add(t[n])
sel = {}
for l in open(sys.argv[2]):
    o = json.loads(l); sel[o["commit"]] = {x for w in o["workers"] for x in w}
miss = {i: len(fail[i] - sel[i]) for i in done if fail[i] - sel[i]}
print("labeled", len(done), "| with failures", sum(1 for i in done if fail[i]), "| MISSES", miss or "none")
for i in done: print(" ", i, "failing", len(fail[i]), "selected", len(sel[i]))
