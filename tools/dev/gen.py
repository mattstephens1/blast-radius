"""Generate synthetic one-token mutation patches over sqlglot_src, like the sample."""
import random, re, sys, difflib, pathlib
repo = pathlib.Path(sys.argv[1]); out = pathlib.Path(sys.argv[2]); n = int(sys.argv[3])
random.seed(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
out.mkdir(parents=True, exist_ok=True)
for p in out.glob("*.patch"): p.unlink()
files = [p for p in repo.glob("sqlglot/**/*.py")]
weights = [len(p.read_text()) for p in files]
MUTS = [
    (r"(?<![<>=!])<(?![<=])", "<="), (r"<=", "<"), (r"(?<![<>=!-])>(?![>=])", ">="), (r">=", ">"),
    (r"\band\b", "or"), (r"\bor\b", "and"), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"==", "!="), (r"!=", "=="), (r"\bis not\b", "is"), (r"\bnot in\b", "in"),
    (r"(?<![\w.])(\d+)(?![\w.])", None), (r'"([A-Z_]{2,})"', None),
]
made = 0; tries = 0
while made < n and tries < n * 50:
    tries += 1
    p = random.choices(files, weights)[0]
    lines = p.read_text().split("\n")
    kind = random.random()
    if kind >= 0.08:
        pat, rep = random.choice(MUTS)
        cands = [j for j, x in enumerate(lines) if x.strip() and not x.strip().startswith(("#", '"', "'")) and re.search(pat, x)]
        if not cands: continue
        i = random.choice(cands)
    else:
        i = random.randrange(len(lines))
    L = lines[i]; s = L.strip()
    if not s: continue
    new = None
    if kind < 0.08:
        new_lines = lines[:i] + [re.match(r"\s*", L).group() + "# Kept for backwards compatibility"] + lines[i:]
    else:
        ms = list(re.finditer(pat, L))
        if not ms: continue
        m = random.choice(ms)
        if rep is None:
            g = m.group(1)
            rep = str(int(g) + 1) if g.isdigit() else g[:-1] + chr(ord(g[-1]) % 26 + 65) if False else (str(int(g)+1) if g.isdigit() else m.group(0).replace(g, g + "X"))
            new = L[:m.start()] + (rep if g.isdigit() else rep) + L[m.end():] if g.isdigit() else L[:m.start()] + rep + L[m.end():]
        else:
            new = L[:m.start()] + rep + L[m.end():]
        new_lines = lines[:i] + [new] + lines[i + 1:]
    rel = str(p.relative_to(repo))
    d = "".join(difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new_lines],
                                      "a/" + rel, "b/" + rel, n=3))
    if not d: continue
    (out / f"s{made:04d}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + d)
    made += 1
print(made)
