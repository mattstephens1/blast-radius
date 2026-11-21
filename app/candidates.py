"""Turn a patch into the set of tests that could notice it.

The rule throughout: when unsure, select more. A missed failing test costs
300 s; running every test costs about 37 s.
"""
import re

import analyse
import patchlib

FAMILY_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")
BIG = 10**7


class Maps:
    def __init__(self, lines, import_lines, opened, unattributed_opens, tests, repo, mentions=None):
        self.lines = {f: {int(k): set(v) for k, v in m.items()} for f, m in lines.items()}
        self.import_lines = {f: set(v) for f, v in import_lines.items()}
        self.opened = opened                     # fixture path -> set of test numbers
        self.unattributed = set(unattributed_opens)  # files opened while no test was running
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        self.all = {t["number"] for t in tests}
        self.repo = repo
        self.mentions = mentions or {}
        self._src = {}

    def covering(self, path, lo=1, hi=BIG):
        out = set()
        for ln, nums in self.lines.get(path, {}).items():
            if lo <= ln <= hi:
                out |= nums
        return out

    def runs_at_import(self, path, lo, hi):
        return any(lo <= ln <= hi for ln in self.import_lines.get(path, ()))

    def source_lines(self):
        if not self._src:
            for p in self.repo.glob("sqlglot/**/*.py"):
                self._src[str(p.relative_to(self.repo))] = p.read_text().split("\n")
        return self._src

    def referencing(self, names):
        """Tests that ran any sqlglot line mentioning one of these identifiers.

        A mention that only runs at import time (e.g. inside a class-level dict)
        in a dialect module brings in that dialect's whole family. One in a
        shared core module adds nothing by itself: the runtime lines that read
        such tables are what tests execute.
        """
        if not names:
            return set()
        pat = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b")
        out = set()
        for path, lines in self.source_lines().items():
            cov = self.lines.get(path, {})
            hit_import = False
            for i, text in enumerate(lines, 1):
                if pat.search(text):
                    if i in cov:
                        out |= cov[i]
                    else:
                        hit_import = True
            if hit_import:
                out |= family(path, self) or set()
        return out


def _dialect_of(path):
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in FAMILY_DIRS \
            and parts[2].endswith(".py") and parts[2] != "__init__.py":
        return parts[2][:-3]
    return None


def _subclass_dialects(name, maps):
    """Dialects whose modules import this dialect's modules (e.g. redshift <- postgres)."""
    found, todo = {name}, [name]
    src = maps.source_lines()
    while todo:
        cur = todo.pop()
        pat = re.compile(r"sqlglot\.(?:%s)\.%s\b" % ("|".join(FAMILY_DIRS), re.escape(cur)))
        for path, lines in src.items():
            other = _dialect_of(path)
            if other and other not in found and any(pat.search(t) for t in lines):
                found.add(other)
                todo.append(other)
    return found


def family(path, maps):
    """Every test that touches a dialect: ran any of its modules, or names it.

    Includes dialects built on top of it (a change to postgres reaches redshift).
    Returns None for files that are not dialect-specific.
    """
    name = _dialect_of(path)
    if name is None or name == "dialect":
        return None
    out = set()
    for dialect in _subclass_dialects(name, maps):
        for d in FAMILY_DIRS:
            out |= maps.covering(f"sqlglot/{d}/{dialect}.py")
        out |= set(maps.mentions.get(dialect, ()))
        out |= _borrowers(dialect, maps)
    return out


def _borrowers(dialect, maps):
    """Tests that run code elsewhere naming this dialect, e.g. presto parsing
    TO_CHAR with build_formatted_time(..., "teradata")."""
    pat = re.compile(r"[\"']%s[\"']|\b%s\b" % (re.escape(dialect), re.escape(dialect.capitalize())), re.I)
    out = set()
    for path, lines in maps.source_lines().items():
        if _dialect_of(path) == dialect:
            continue
        cov = maps.lines.get(path, {})
        import_hit = False
        for i, text in enumerate(lines, 1):
            if pat.search(text):
                if i in cov:
                    out |= cov[i]
                else:
                    import_hit = True
        if import_hit and _dialect_of(path) not in (None, "dialect"):
            out |= maps.covering(path)
    return out


def python_change(path, old, new, maps, notes):
    d = analyse.diff_python(old, new)
    if d is None:
        notes.append(("syntax error -> all", path))
        return set(maps.all)
    if d.same:
        notes.append(("no code change", path))
        return set()
    sel = set()
    fam = family(path, maps)
    broad = False  # an import-time change: needs the dialect family, or everything

    for q, node in d.functions:
        lo, hi = analyse.body_range(node)
        sel |= maps.covering(path, lo, hi)
        if maps.runs_at_import(path, lo, hi):
            broad = True
            notes.append(("function runs at import", q))
        else:
            notes.append(("function", q))
    for q, node in d.classes:
        sel |= maps.covering(path, node.lineno, node.end_lineno)
        notes.append(("class body", q))
        broad = True
    if (d.class_names or d.classes) and fam is None:
        # a shared class changed at import time: follow every use of its name
        names = d.class_names | {q.rsplit(".", 1)[-1] for q, _ in d.classes}
        refs = maps.referencing(names)
        if not refs:
            notes.append(("class change with no known users -> all", path))
            return set(maps.all)
        sel |= refs
    for q in d.added:
        owner = q.rsplit(".", 1)[0] if "." in q else ""
        node = d.units.get(owner)
        if node is not None:
            sel |= maps.covering(path, node.lineno, node.end_lineno)
        notes.append(("added", q))
        broad = True
    if d.module_names:
        sel |= maps.covering(path)
        if fam is None:
            sel |= maps.referencing(d.module_names)
        notes.append(("module names", sorted(d.module_names)))
        broad = True
    if d.module_other:
        if fam is not None:
            notes.append(("module statement in dialect", path))
            sel |= fam
        else:
            notes.append(("module statement -> all", path))
            return set(maps.all)

    if broad:
        if fam is not None:
            sel |= fam
        elif d.module_names or d.added or any(maps.runs_at_import(path, *analyse.body_range(n))
                                              for _, n in d.functions):
            # shared machinery that runs at import: anything could depend on it
            notes.append(("import-time core change -> all", path))
            return set(maps.all)
    return sel


def test_file_change(path, old, new, maps, notes):
    """A test module changed. Edits inside test methods affect that file only;
    anything else (helpers, base classes such as Validator in test_dialect.py)
    also reaches every test file that imports this one."""
    own = maps.by_file.get(path)
    if not own or not path.split("/")[-1].startswith("test_"):
        notes.append(("test helper -> all", path))
        return set(maps.all)
    d = analyse.diff_python(old, new)
    if d is None:
        notes.append(("test file syntax error", path))
        return set(own) | importers(path, maps)
    if d.same:
        notes.append(("no code change", path))
        return set()
    only_tests = (not d.classes and not d.module_names and not d.module_other
                  and all(q.rsplit(".", 1)[-1].startswith("test") for q, _ in d.functions)
                  and all(q.rsplit(".", 1)[-1].startswith("test") for q in d.added))
    notes.append(("test file", path, "tests only" if only_tests else "shared code"))
    return set(own) if only_tests else set(own) | importers(path, maps)


def importers(path, maps):
    """Tests in any test file that imports this test module."""
    module = path[:-3].replace("/", ".")
    pat = re.compile(r"^\s*(from\s+%s\s+import|import\s+%s\b)" % (re.escape(module), re.escape(module)), re.M)
    out = set()
    for p in maps.repo.glob("tests/**/*.py"):
        rel = str(p.relative_to(maps.repo))
        if rel != path and pat.search(p.read_text()):
            out |= maps.by_file.get(rel, set())
    return out


def candidates(patch_text, repo, maps):
    """Return (set of tests, notes)."""
    sel, notes = set(), []
    for f in patchlib.parse(patch_text):
        path = f["old"] if f["old"] != "/dev/null" else f["new"]
        src = repo / path
        old = src.read_text() if src.exists() else ""
        if path.startswith("sqlglot/") and path.endswith(".py"):
            sel |= python_change(path, old, patchlib.apply(old, f["hunks"]), maps, notes)
        elif path.startswith("tests/") and path.endswith(".py"):
            sel |= test_file_change(path, old, patchlib.apply(old, f["hunks"]), maps, notes)
        elif path.startswith("tests/"):
            users = maps.opened.get(path)
            if users and path not in maps.unattributed:
                sel |= users
                notes.append(("fixture", path, len(users)))
            else:
                sel |= maps.all
                notes.append(("fixture with unknown readers -> all", path))
        else:
            sel |= maps.all
            notes.append(("other file -> all", path))
    return sel, notes
