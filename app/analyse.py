"""Compare the old and new versions of a Python file by syntax tree."""
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
            else:
                walk(child, prefix)

    walk(tree, "")
    return units


def _plain(body):
    """Statements in a body that are not function or class definitions."""
    return [s for s in body if not isinstance(s, DEFS)]


def _names(stmts):
    """Names assigned by statements (NAME = ..., NAME: T = ...)."""
    out = set()
    for s in stmts:
        targets = s.targets if isinstance(s, ast.Assign) else [getattr(s, "target", None)]
        for t in targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
    return out


def _changed_stmts(old_body, new_body):
    """Plain statements that differ between two bodies, as (old_or_new_stmt)."""
    a = {ast.dump(s): s for s in _plain(old_body)}
    b = {ast.dump(s): s for s in _plain(new_body)}
    return [s for k, s in a.items() if k not in b] + [s for k, s in b.items() if k not in a]


def body_range(node):
    """Lines of a function's body, skipping its docstring and def/decorator lines."""
    return node.body[0].lineno, node.end_lineno


class Diff:
    """What changed between two versions of one file."""

    def __init__(self):
        self.same = False
        self.functions = []      # (qualname, old node) whose code changed or was removed
        self.added = []          # qualnames of functions/classes that are new
        self.classes = []        # (qualname, old node) whose class-level statements changed
        self.class_names = set()     # attribute names changed inside those classes
        self.module_names = set()    # module-level names whose assignment changed
        self.module_other = False    # a module-level statement that is not a plain assignment changed
        self.units = {}


def _strip_docstrings(tree):
    """Docstrings never run in the test list (no doctests), so ignore them."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module,) + DEFS) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass(lineno=first.lineno, col_offset=0,
                                                       end_lineno=first.end_lineno, end_col_offset=0)]
    return tree


def diff_python(old_src, new_src):
    """Return a Diff, or None if either version does not parse."""
    try:
        old_t, new_t = _strip_docstrings(ast.parse(old_src)), _strip_docstrings(ast.parse(new_src))
    except SyntaxError:
        return None
    d = Diff()
    if ast.dump(old_t) == ast.dump(new_t):
        d.same = True
        return d
    ou, nu = _units(old_t), _units(new_t)
    d.units = ou
    for q, node in ou.items():
        n2 = nu.get(q)
        if isinstance(node, FUNCS):
            if n2 is None or ast.dump(n2) != ast.dump(node):
                d.functions.append((q, node))
        elif isinstance(node, ast.ClassDef):
            if n2 is None:
                d.classes.append((q, node))
                continue
            changed = _changed_stmts(node.body, n2.body)
            header = lambda c: [ast.dump(x) for x in c.bases + c.keywords + c.decorator_list]
            if changed or header(node) != header(n2):
                d.classes.append((q, node))
                d.class_names |= _names(changed)
    d.added = [q for q in nu if q not in ou]
    changed = _changed_stmts(old_t.body, new_t.body)
    for s in changed:
        if isinstance(s, (ast.Assign, ast.AnnAssign)):
            d.module_names |= _names([s])
        else:
            d.module_other = True
    return d
