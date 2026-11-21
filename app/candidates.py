"""Turn a patch into a candidate set of test numbers."""
import ast
import patchlib, analyse


class Maps:
    def __init__(self, linemap, opened, tests):
        self.lines = {f: {int(k): v for k, v in m.items()} for f, m in linemap.items()}
        self.opened = {}
        num = {t["id"]: t["number"] for t in tests}
        for tid, files in opened.items():
            if tid in num:
                for f in files:
                    self.opened.setdefault(f, set()).add(num[tid])
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        self.all = {t["number"] for t in tests}

    def covering(self, path, lo, hi):
        m = self.lines.get(path, {})
        out = set()
        for ln in range(lo, hi + 1):
            out.update(m.get(ln, ()))
        return out


FAMILY_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")


def family(path, maps):
    """A class-level edit in e.g. dialects/tsql.py affects every test that runs any tsql module."""
    parts = path.split("/")
    if len(parts) != 3 or parts[1] not in FAMILY_DIRS or parts[2] in ("__init__.py", "dialect.py"):
        return set()
    out = set()
    for d in FAMILY_DIRS:
        out |= maps.covering(f"sqlglot/{d}/{parts[2]}", 1, 10**6)
    return out


def candidates(patch_text, repo, maps):
    """Return (set of tests, list of notes). repo is a Path to pinned sqlglot."""
    sel, notes = set(), []
    for f in patchlib.parse(patch_text):
        path = f["old"] if f["old"] != "/dev/null" else f["new"]
        src = repo / path
        old = src.read_text() if src.exists() else ""
        if path.startswith("sqlglot/") and path.endswith(".py"):
            new = patchlib.apply(old, f["hunks"])
            r = analyse.diff_python(old, new)
            if r is None:  # broken syntax: everything importing it fails
                sel |= maps.all
                notes.append(("syntax", path)); continue
            if r[2]:
                notes.append(("same", path)); continue
            ranges, imp, _, units = r
            for lo, hi, q in ranges:
                sel |= maps.covering(path, lo, hi)
            for q in imp:
                if q == "":
                    sel |= maps.covering(path, 1, 10**6); notes.append(("module", path))
                else:
                    node = units[q]
                    sel |= maps.covering(path, node.lineno, node.end_lineno)
                    sel |= family(path, maps)
                    notes.append(("class", path, q))
        elif path.startswith("tests/") and path.endswith(".py"):
            sel |= maps.by_file.get(path, set()) or maps.all
            notes.append(("testfile", path))
        elif path.startswith("tests/"):
            sel |= maps.opened.get(path, set())
            notes.append(("fixture", path, len(maps.opened.get(path, ()))))
        else:
            sel |= maps.all  # unknown kind of file: be safe
            notes.append(("other", path))
    return sel, notes
