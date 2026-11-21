import coverage, json, os, sys
cd = coverage.CoverageData('/home/claude/tools/.cov'); cd.read()
tests = json.load(open(sys.argv[1]))['tests']
num = {t['id']: t['number'] for t in tests}
root = '/home/claude/sqlglot/'
out = {}
for f in cd.measured_files():
    ctx = cd.contexts_by_lineno(f)
    out[f[len(root):]] = {ln: sorted({num[c] for c in cs if c in num}) for ln, cs in ctx.items()}
json.dump(out, open('/home/claude/tools/linemap.json','w'))
