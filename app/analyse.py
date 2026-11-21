"""Work out which tests a patch could affect, using maps from the pinned commit."""
import ast


def _units(tree):
    """qualname -> (node, start, end) for every function/class; plus a skeleton dump."""
    units = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + child.name
                units[q] = child
                walk(child, q + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return units


def _strip_functions(tree):
    """Dump of the tree with every function body emptied: shows import-time changes."""
    class S(ast.NodeTransformer):
        def visit_FunctionDef(self, n):
            n.body = [ast.Pass()]
            return n
        visit_AsyncFunctionDef = visit_FunctionDef
    import copy
    return ast.dump(S().visit(copy.deepcopy(tree)))


def _first_stmt_lines(node):
    return node.lineno, node.end_lineno


def diff_python(old_src, new_src):
    """Return (changed_old_ranges, import_level_classes, all_same)."""
    try:
        old_t, new_t = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return None
    if ast.dump(old_t) == ast.dump(new_t):
        return [], set(), True
    ou, nu = _units(old_t), _units(new_t)
    ranges, import_level = [], set()
    for q, node in ou.items():
        if isinstance(node, ast.ClassDef):
            continue
        n2 = nu.get(q)
        if n2 is None or ast.dump(n2) != ast.dump(node):
            ranges.append((node.lineno, node.end_lineno, q))
    added = [q for q, n in nu.items() if q not in ou and not isinstance(n, ast.ClassDef)]
    # import-time (class bodies / module level) changes
    def owner(q):
        return q.rsplit(".", 1)[0] if "." in q else ""
    for q in added:
        import_level.add(owner(q))
    for q, node in ou.items():
        if isinstance(node, ast.ClassDef):
            n2 = nu.get(q)
            if n2 is None or _strip_functions(n2) != _strip_functions(node):
                # only flag if the class's own direct non-function body changed
                a = [ast.dump(s) for s in node.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                b = [ast.dump(s) for s in (n2.body if n2 else []) if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                if a != b or n2 is None:
                    import_level.add(q)
    a = [ast.dump(s) for s in old_t.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    b = [ast.dump(s) for s in new_t.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if a != b:
        import_level.add("")
    return ranges, import_level, False, ou
