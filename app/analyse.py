"""Compare the old and new versions of a Python file by syntax tree.

Only the parts of the file the patch touched are compared: everything else is
byte-for-byte the same, so it cannot have changed. Comments are invisible to
the syntax tree, and docstrings are dropped before comparing (no doctests are
in the test list).
"""
import ast

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _units(tree):
    """qualified name -> node, for every function and class."""
    units = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFS):
                q = prefix + child.name
                units[q] = child
                walk(child, q + ".")
            elif isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                walk(child, prefix)

    walk(tree, "")
    return units


def _is_doc(stmt):
    return isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant) \
        and isinstance(stmt.value.value, str)


def _strip_docstrings(node):
    for n in ast.walk(node):
        if isinstance(n, (ast.Module,) + DEFS) and n.body and _is_doc(n.body[0]):
            first = n.body[0]
            n.body = n.body[1:] or [ast.Pass(lineno=first.lineno, col_offset=0,
                                              end_lineno=first.end_lineno, end_col_offset=0)]
    return node


def _singleton(node):
    """None, or an ALL_CAPS attribute such as TokenType.SELECT (an enum member)."""
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return isinstance(node, ast.Attribute) and node.attr.isupper() and len(node.attr) > 1


class _Canonical(ast.NodeTransformer):
    """Rewrite `x is None` as `x == None` (and `is not` as `!=`), likewise for enum
    members. In sqlglot these mean the same thing: no class that meets None or an
    enum member there has an __eq__ that disagrees with identity."""

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and (_singleton(node.left) or _singleton(node.comparators[0])):
            op = node.ops[0]
            if isinstance(op, ast.Is):
                node.ops = [ast.Eq()]
            elif isinstance(op, ast.IsNot):
                node.ops = [ast.NotEq()]
        return node


def _dump(node, cache):
    key = id(node)
    if key not in cache:
        cache[key] = ast.dump(_Canonical().visit(_strip_docstrings(node)))
    return cache[key]


def _plain(body):
    """Statements in a body that are not function or class definitions, nor docstrings."""
    return [s for s in body if not isinstance(s, DEFS) and not _is_doc(s)]


def _names(stmts):
    """Names assigned by statements (NAME = ..., NAME: T = ...)."""
    out = set()
    for s in stmts:
        targets = s.targets if isinstance(s, ast.Assign) else [getattr(s, "target", None)]
        for t in targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
    return out


def _touches(node, lines):
    lo = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
    return any(lo <= ln <= node.end_lineno for ln in lines)


def _changed_stmts(old_body, new_body, old_lines, new_lines, cache):
    """Plain statements that differ, looking only at those the patch touched."""
    old_touched = [s for s in _plain(old_body) if _touches(s, old_lines)]
    new_touched = [s for s in _plain(new_body) if _touches(s, new_lines)]
    if not old_touched and not new_touched:
        return []
    every_old = {_dump(s, cache) for s in _plain(old_body)}
    every_new = {_dump(s, cache) for s in _plain(new_body)}
    return [s for s in old_touched if _dump(s, cache) not in every_new] + \
        [s for s in new_touched if _dump(s, cache) not in every_old]


def body_range(node):
    """Lines of a function's body, skipping its docstring and def/decorator lines."""
    body = [s for s in node.body if not _is_doc(s)] or node.body
    return body[0].lineno, node.end_lineno


def stmt_ranges_for_lines(source, changed_lines, func_lo, func_hi):
    """Statement ranges covering the given changed lines within a function body.

    Returns a list of (lo, hi) pairs — the innermost statements that contain
    each changed line — or None if the source does not parse.  Using statement
    ranges rather than the full function body avoids pulling in tests that only
    ran other parts of the same function.
    """
    if not changed_lines:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    result = set()
    for ln in changed_lines:
        if not (func_lo <= ln <= func_hi):
            continue
        best = None
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt) and func_lo <= node.lineno and node.end_lineno <= func_hi:
                if node.lineno <= ln <= node.end_lineno:
                    if best is None or node.lineno >= best.lineno:
                        best = node
        if best:
            result.add((best.lineno, best.end_lineno))
    return list(result) if result else None


class Diff:
    """What changed between two versions of one file."""

    def __init__(self):
        self.same = False
        self.functions = []      # (qualname, old node) whose code changed or was removed
        self.added = []          # qualnames of functions/classes that are new
        self.classes = []        # (qualname, old node) whose class-level statements changed
        self.class_names = set()     # attribute names changed inside those classes
        self.class_unnamed = False   # a changed class-level statement that is not NAME = ...
        self.module_names = set()    # module-level names whose assignment changed
        self.module_other = False    # a module-level statement that is not a plain assignment changed
        self.units = {}


def diff_python(old_src, new_src, old_lines=None, new_lines=None):
    """Return a Diff, or None if either version does not parse.

    old_lines / new_lines: line numbers the patch touched in each version
    (None = compare everything).
    """
    try:
        old_t, new_t = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return None
    every = range(1, 10**7)
    old_lines = every if old_lines is None else old_lines
    new_lines = every if new_lines is None else new_lines
    cache = {}
    d = Diff()
    ou, nu = _units(old_t), _units(new_t)
    d.units = ou
    header = lambda c: [ast.dump(x) for x in c.bases + c.keywords + c.decorator_list]
    for q, node in ou.items():
        n2 = nu.get(q)
        if n2 is not None and not _touches(node, old_lines) and not _touches(n2, new_lines):
            continue  # untouched by the patch
        if isinstance(node, FUNCS):
            if n2 is None or not isinstance(n2, FUNCS) or _dump(n2, cache) != _dump(node, cache):
                d.functions.append((q, node))
        else:
            if n2 is None or not isinstance(n2, ast.ClassDef):
                d.classes.append((q, node))
                d.class_unnamed = True
                continue
            changed = _changed_stmts(node.body, n2.body, old_lines, new_lines, cache)
            if changed or header(node) != header(n2):
                d.classes.append((q, node))
                d.class_names |= _names(changed)
                if header(node) != header(n2) or any(
                        not isinstance(x, (ast.Assign, ast.AnnAssign)) or not _names([x]) for x in changed):
                    d.class_unnamed = True
    d.added = [q for q in nu if q not in ou]
    for s in _changed_stmts(old_t.body, new_t.body, old_lines, new_lines, cache):
        if isinstance(s, (ast.Assign, ast.AnnAssign)):
            d.module_names |= _names([s])
        else:
            d.module_other = True
    d.same = not (d.functions or d.classes or d.added or d.module_names or d.module_other)
    return d
