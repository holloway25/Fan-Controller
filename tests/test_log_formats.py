"""Every _log call formats without raising.

WHY THIS EXISTS: adding fault codes to the restart lines produced

    _log("... up to %%ds of " % CODE + "readings will be lost" % SECS)

which compiles, passes every test, and raises TypeError the moment it
runs - because % binds tighter than +, so the second fragment had a
format applied to a string with no format spec in it. It sits on the
restart path, which no test exercises, so it would have surfaced as a
restart that half-worked months from now.

The tests below cannot catch that shape by calling the functions -
reaching a "could not enumerate descriptors" branch means breaking the
process. So this walks the SOURCE instead and formats every _log call
that has a literal format string, which is cheap and catches the whole
class.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "controller.py")


def _log_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = getattr(f, "id", None) or getattr(f, "attr", None)
        if name == "_log" and node.args:
            yield node


def test_no_log_call_mixes_a_format_with_a_concatenation():
    """The shape that broke: "..." % X + "..." % Y.

    Legitimate concatenation of two plain literals is fine. A formatted
    fragment joined to another fragment is not - the operators do not
    group the way it reads.
    """
    tree = ast.parse(open(SRC).read())
    bad = []
    for call in _log_calls(tree):
        arg = call.args[0]
        if not isinstance(arg, ast.BinOp) or not isinstance(arg.op, ast.Add):
            continue
        for side in (arg.left, arg.right):
            if isinstance(side, ast.BinOp) and isinstance(side.op, ast.Mod):
                bad.append(call.lineno)
    assert not bad, (
        "_log at line(s) %s concatenates a formatted fragment; %% binds "
        "tighter than + and the halves do not group as written"
        % ", ".join(str(n) for n in sorted(set(bad))))
    return "no format-then-concatenate"


def test_every_literal_format_string_has_matching_placeholders():
    """Count the specs and count the values."""
    tree = ast.parse(open(SRC).read())
    checked = 0
    for call in _log_calls(tree):
        arg = call.args[0]
        if not (isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod)):
            continue
        left = arg.left
        # Only literal format strings: anything built at runtime is out
        # of scope here and is covered by the calls the suite makes.
        if not (isinstance(left, ast.Constant)
                and isinstance(left.value, str)):
            continue
        fmt = left.value
        specs = 0
        i = 0
        while i < len(fmt):
            if fmt[i] == "%":
                if i + 1 < len(fmt) and fmt[i + 1] == "%":
                    i += 2
                    continue
                specs += 1
            i += 1
        right = arg.right
        if isinstance(right, ast.Tuple):
            given = len(right.elts)
        else:
            given = 1
        assert specs == given, (
            "_log at line %d has %d placeholder(s) and %d value(s): %r"
            % (call.lineno, specs, given, fmt))
        checked += 1
    assert checked > 10, "only checked %d calls - did the walk break?" % checked
    return "%d format strings balanced" % checked


def test_the_checker_can_actually_fail():
    """A checker that cannot fail proves nothing."""
    tree = ast.parse('_log("%s and %s" % (one,))')
    call = list(_log_calls(tree))[0]
    arg = call.args[0]
    assert isinstance(arg.right, ast.Tuple)
    assert len(arg.right.elts) == 1
    fmt = arg.left.value
    assert fmt.count("%s") == 2, "the probe is wrong"
    return "the checker bites"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
