import json,sys,collections,re
S=sys.argv[1]
tests=json.load(open("sample/tests.json"))["tests"]; d={t["number"]:round(t["seconds"]*1000) for t in tests}
notes={l.split()[0]:l.strip() for l in open(S+"/synth_err.txt") if re.match(r"s\d{4} ",l)}
rows=[]
for l in open(S+"/synth_out.jsonl"):
    o=json.loads(l); w=[x for x in o["workers"] if x]
    c=(max(sum(d[t] for t in x) for x in w)+2000*len(w)) if w else 0
    rows.append((c,o["commit"],notes.get(o["commit"],"")[:170]))
rows.sort(reverse=True)
tot=sum(r[0] for r in rows); print("total",tot/1000,"mean",tot/len(rows)/1000)
cat=collections.defaultdict(lambda:[0,0])
for c,k,n in rows:
    m=re.findall(r"\('([^']+)'",n); key=" | ".join(sorted(set(m)))
    cat[key][0]+=c; cat[key][1]+=1
for k,v in sorted(cat.items(),key=lambda x:-x[1][0])[:25]: print(f"{v[0]/1000:8.1f}s {v[1]:4d}  {k}")
print()
for r in rows[:40]: print(r[0]/1000, r[2])
