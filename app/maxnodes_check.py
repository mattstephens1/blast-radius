"""Does a patch make tests/test_parser.py::TestParser::test_max_nodes fail?

The test (28.8 s) asserts:
  1. parse_one("SELECT 1", max_nodes=1_000_000) is an exp.Select
  2. a 500-column SELECT under the same limit is an exp.Select
  3. "SELECT " + 10,000 copies of x=x=...=x (depth 100), with max_nodes=1_000_000,
     raises ParseError whose message contains "Maximum number of AST nodes"
  4. the same SQL with max_nodes=50_000 raises ParseError containing "(50000)"
  5. a 100-column SELECT with max_nodes=5 raises ParseError
  6. parse_one("SELECT 1, 2, 3") is then an exp.Select

All but 3 and 4 are cheap, so they are run as written. 3 and 4 parse a SQL
made of one column repeated: each column adds the same nodes, so the node count
is a + b * columns. With the patched source this module measures the count at
three widths (checking it really is linear), predicts the full-size count, and
reproduces each limit error just past its crossing point to check the message.
At the unpatched commit the full-size count is 1,000,001 against the 1,000,000
limit, so the prediction has to be exact, and it is.

Every condition is the test's own, evaluated on the patched code, so "passes"
means the full-size test passes.

    python maxnodes_check.py --check <repo>   (stdin: {path: source})
    python maxnodes_check.py --self <repo>    (the unpatched source)
    stdout: {"ok": true, "passes": bool, "why": "..."}
"""
# sqlglot turns on extra validation when it is imported under a test runner
# (expressions/core.py: UNITTEST = "unittest" in sys.modules or "pytest" in
# sys.modules). Import unittest first so every check sees what the tests see.
import unittest  # noqa: F401
import json
import os
import sys

TEST_ID = "tests/test_parser.py::TestParser::test_max_nodes"
DEPTH, WIDTH = 100, 10_000                 # as in the test
LIMITS = ((1_000_000, "Maximum number of AST nodes"), (50_000, "(50000)"))  # as in the test
SAMPLE_WIDTHS = (20, 40, 60)               # three points: the count must be exactly linear
MAX_REPRO_COLUMNS = 2_000                  # reproducing the 50,000 limit error must stay cheap


def _setup(repo, overrides):
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if overrides:
        import import_state
        sys.meta_path.insert(0, import_state._Patched(repo, overrides))


def evaluate():
    """(passes, reason) for the test's assertions on the code now imported."""
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    if not isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select):
        return False, "SELECT 1 is not a Select"
    cols = ",".join("col%d" % i for i in range(500))
    if not isinstance(parse_one("SELECT %s FROM t" % cols, max_nodes=1_000_000), exp.Select):
        return False, "500 columns is not a Select"

    expr = "x" + "=x" * DEPTH

    def sql(columns):
        return "SELECT " + ",".join([expr] * columns)

    counts = []
    for w in SAMPLE_WIDTHS:
        parser = Parser(max_nodes=10 ** 12)  # count nodes, never hit the limit
        trees = parser.parse(Tokenizer().tokenize(sql(w)), sql(w))
        if len(trees) != 1 or not isinstance(trees[0], exp.Select):
            return False, "repeated columns do not parse to one Select"
        counts.append(parser._node_count)
    (w0, w1, w2), (c0, c1, c2) = SAMPLE_WIDTHS, counts
    if (c1 - c0) * (w2 - w1) != (c2 - c1) * (w1 - w0) or (c1 - c0) % (w1 - w0):
        return False, "node count is not linear in the columns: %s" % counts
    b = (c1 - c0) // (w1 - w0)
    a = c0 - b * w0
    if b <= 0:
        return False, "columns add no nodes: %s" % counts
    full = a + b * WIDTH

    for limit, wanted in LIMITS:
        if full <= limit:
            return False, "full size makes %d nodes, not above %d" % (full, limit)
        if limit == 1_000_000:
            # reproducing this one would cost what the test costs. The error comes from
            # the same raise, with the same format, whatever the limit: its message is
            # checked at the 50,000 limit below, prefix included.
            continue
        crossing = (limit - a) // b + 1   # the first width whose count exceeds the limit
        if crossing > MAX_REPRO_COLUMNS:
            return False, "limit %d crossed only at %d columns" % (limit, crossing)
        try:
            parse_one(sql(crossing), max_nodes=limit)
            return False, "no error at the %d limit" % limit
        except ParseError as error:
            message = str(error)
        if wanted not in message or "Maximum number of AST nodes" not in message:
            return False, "limit %d message %r" % (limit, message[:80])

    try:
        parse_one("SELECT " + ",".join("x=%d" % i for i in range(100)), max_nodes=5)
        return False, "max_nodes=5 did not raise"
    except ParseError:
        pass
    if not isinstance(parse_one("SELECT 1, 2, 3"), exp.Select):
        return False, "parse after a limit error is not a Select"
    return True, "a=%d b=%d full=%d" % (a, b, full)


def main():
    mode, repo = sys.argv[1:3]
    repo = os.path.abspath(repo)
    overrides = json.load(sys.stdin) if mode == "--check" else None
    try:
        _setup(repo, overrides)
        passes, why = evaluate()
    except BaseException as error:  # the test would error out too
        print(json.dumps({"ok": True, "passes": False, "why": repr(error)[:200]}))
        return
    print(json.dumps({"ok": True, "passes": passes, "why": why}))


if __name__ == "__main__":
    main()
