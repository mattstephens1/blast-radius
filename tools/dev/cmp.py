import json,collections,sys
t={x["id"]:x["number"] for x in json.load(open("sample/tests.json"))["tests"]}
got=collections.defaultdict(set)
for f in sys.argv[1:]:
    for l in open(f):
        i,n=l.split(maxsplit=1); n=n.strip()
        if n not in ("DONE","PATCHFAIL"): got[i].add(t.get(n,n))
base=got.pop("empty",set())
print("baseline failures:",base)
json.dump({k:sorted(v-base,key=str) for k,v in got.items()},open(sys.argv[1]+".json","w"))
