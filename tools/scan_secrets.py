#!/usr/bin/env python3
"""Find credential assignments with real-looking values.

ONE IMPLEMENTATION, TWO CALLERS. The pre-commit hook and the privacy
workflow both run this. Before it existed the hook was the only
credential check anywhere, so `git commit --no-verify` walked a secret
straight past a green CI - reported by review against 40f2d3f.

WHY NOT A SHELL REGEX. The hook's grep treated a value as everything up
to the first space, so a password assigned a QUOTED VALUE CONTAINING
SPACES passed cleanly - the check saw the first word and stopped.

Quoting is the whole problem, and matching balanced quotes with spaces
inside them is where shell one-liners stop being readable. This reads
the quote properly.

The offending line is described rather than written out. Spelled out,
this docstring trips the very rule it explains and the file cannot be
committed without --no-verify - which is how a check teaches people to
skip it. tests/test_secret_scanner.py has the real strings, assembled
at runtime for the same reason.

NOTHING SITE-SPECIFIC LIVES HERE. This file is public. It knows what a
credential ASSIGNMENT looks like, never what any actual secret is. For
site strings see the local-patterns file described in the hook.

Usage:
    scan_secrets.py --staged           added lines in the index
    scan_secrets.py --tree             every tracked file at HEAD
    scan_secrets.py --commit SHA       every file as of one commit

Exit 0 clean, 1 on a finding, 2 on a usage error.
"""
import argparse
import io
import re
import subprocess
import sys

# "auth" and "credential" were in here and produced nothing but noise -
# auth="CRAM-MD5 PLAIN LOGIN" is a capability list, not a secret. A
# scanner people learn to ignore protects nothing.
KEY = r"(?:password|passwd|pwd|api[_-]?key|apikey|token|secret)"

# The operator must be a real assignment. Without the lookarounds, ===
# in JavaScript matched and every comparison against "SET" in the
# dashboard was reported.
ASSIGN = re.compile(
    r"(?:\"%s\"|'%s'|%s)\s*(?<![=!<>])([:=])(?![=])\s*"
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\s,;}\)]+))" % (KEY, KEY, KEY),
    re.IGNORECASE)

# Interpolation or templating INSIDE a quoted value: "${VAULT_PW}" is a
# reference, not a secret. Applied to quoted and unquoted alike.
INTERP = re.compile(r"\$\{|\$\(|%\(|%[sd]|\{\{|\{\}|<[A-Za-z_]")

# An UNQUOTED value that is code rather than a literal: a call, an
# attribute, a subscript. These are references TO a secret, which is
# what correct code looks like.
#
# NOT APPLIED TO QUOTED VALUES. It used to be, and the bare-identifier
# arm then dismissed every quoted literal that happened to look like a
# word - "s3cr3tvalue" was read as a variable name and waved through.
# Anything inside quotes is a literal by definition.
CODE = re.compile(r"[(\)\[\]$]|\.\w|^\$")

# A bare identifier with no digits, unquoted - a key assigned the name
# of a variable holding it.
# Real unquoted secrets - the YAML case, a key followed by a colon and a
# long alphanumeric run - almost always carry digits, so that is the
# line drawn. A heuristic, and the quoted path above is the one that
# matters. Written out rather than exemplified, for the reason in the
# docstring.
IDENT = re.compile(r"^[A-Za-z_]\w*$")

# Checked against the VALUE ONLY, never the surrounding line: a comment
# saying "example" must not vouch for the secret in front of it.
#
# ANCHORED, AND IT MATTERS. This was a substring search and the list
# included "password" and "secret" - added so the repo's own test value
# would pass - which meant it also excused Password123! and
# mysecretvalue9. A placeholder is a value that is ENTIRELY a marker,
# optionally with a suffix; a real secret that happens to contain the
# word "password" is still a real secret. Reported by review of
# 085e454, and the lesson is the same one as the comment case: an
# exclusion that can appear anywhere excuses everything eventually.
PLACEHOLDER = re.compile(
    r"^(?:"
    r"changeme|change[-_]me|redacted|dummy|sample|fake|todo|tbd|"
    r"xxx+|placeholder|example|test[-_]value|not[-_]a[-_]real"
    r")(?:[-_.].*)?$"
    r"|^your[-_].*$"
    r"|^\.\.\.$",
    re.IGNORECASE)

# Below this, a value is too short to be a credential worth protecting.
MIN_LEN = 6

# EMPTY, AND MEANT TO STAY EMPTY. This held the three files that
# enforce the rules - this scanner, the hook and the privacy workflow -
# on the grounds that they match on their own pattern definitions. They
# do not, as it turns out; only one explanatory comment ever did, and
# rewording it cost nothing.
#
# The exemption was worse than the problem it solved: a credential
# pasted into any of the three would have passed both the tree scan and
# the per-commit scan, and those are exactly the files someone edits
# while thinking about credentials. Reported by review of d9ad884.
#
# If something here ever needs exempting again, reword it instead.
SKIP_PATHS = ()


def _value(m):
    # groups()[0] is the operator; the value is whichever quote arm hit.
    for g in m.groups()[1:]:
        if g is not None:
            return g
    return ""


def suspicious(line):
    """The offending value, or None. Public so the tests can call it."""
    for m in ASSIGN.finditer(line):
        quoted = m.group(2) is not None or m.group(3) is not None
        yaml_style = m.group(1) == ":"
        v = _value(m).strip()
        if len(v) < MIN_LEN:
            continue
        if v in ("SET", "set"):
            continue
        if INTERP.search(v):
            continue
        if not quoted:
            # A STRAY QUOTE IS NOT PART OF THE VALUE. The unquoted arm
            # matches up to whitespace, so prose describing an
            # assignment - a sentence with the whole thing in quotes -
            # left a trailing quote stuck to the value, the identifier
            # test failed on it, and the scanner flagged its own
            # documentation. Stripping quote characters from the ends of
            # an UNQUOTED value cannot hide a secret: a real value
            # ending in a quote is malformed either way.
            v = v.strip("\"'`")
            if len(v) < MIN_LEN:
                continue
            # BOTH CODE EXEMPTIONS ARE CODE-ONLY. CODE ran before the
            # yaml_style check, so a YAML value merely had to LOOK like
            # code to escape: a dotted JWT-shaped token, or anything
            # containing a bracket, went straight through. There are no
            # expressions in a YAML scalar - a dot is part of the value.
            # Reported by review of d9ad884.
            if not yaml_style and CODE.search(v):
                continue
            # THE IDENTIFIER EXEMPTION IS FOR CODE, NOT FOR YAML. It
            # exists because "password = stored_secret" is a reference,
            # which is what correct code looks like. A YAML "password:
            # CorrectHorseBatteryStaple" has the same shape and is a
            # secret in a config file - there are no variables to refer
            # to. Reported by review of 085e454.
            if (not yaml_style and IDENT.match(v)
                    and not any(c.isdigit() for c in v)):
                continue
            if yaml_style and IDENT.match(v) and len(v) < 12:
                # A short bare word after a YAML key is usually an enum
                # - "mode: manual", "level: debug". A passphrase is
                # longer than this, and anything with digits or
                # punctuation is judged on its own merits above.
                continue
        if PLACEHOLDER.search(v):
            continue
        return v
    return None


def _report(where, lineno, line):
    """Say WHERE, never WHAT.

    This printed the offending line. In CI that copies the credential
    into the Actions log - which for this repository is public - so a
    check meant to stop a secret reaching the internet published it
    itself, and to a place with no history rewrite available. Reported
    by review of d9ad884.

    The location is enough: whoever ran it has the file open, and
    whoever reads it in CI should not be told the value.
    """
    print("%s:%s: credential-looking assignment (value redacted)"
          % (where, lineno))


def _git(*args):
    """Run git, and REFUSE TO GUESS if it fails.

    The return code was ignored, so a failed `git show` produced empty
    output that scanned clean and reported success - the worst possible
    failure for a check like this, because it is indistinguishable from
    a genuinely clean tree. Reported by review of 085e454.
    """
    r = subprocess.run(["git"] + list(args), capture_output=True,
                       text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(
            "git %s failed (%d): %s"
            % (" ".join(args), r.returncode, (r.stderr or "").strip()[:200]))
    return r.stdout


def scan_worktree():
    """Tracked files AS THEY ARE ON DISK.

    --tree reads blobs out of HEAD, so it can never see the change in
    front of you: a fix is only visible to it after being committed,
    and a secret added to the working copy is invisible until then too.
    This is the mode a person or a test should use while working;
    --tree and --commit are for history.
    """
    hits = 0
    for path in _paths(_git("ls-files")):
        try:
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except (OSError, ValueError):
            continue                    # deleted, or not readable as text
        for i, line in enumerate(lines, 1):
            if suspicious(line):
                _report(path, i, line)
                hits += 1
    return hits


def scan_staged():
    hits = 0
    path = "(staged)"
    for line in _git("diff", "--cached", "-U0").splitlines():
        if line.startswith("+++"):
            path = line[6:] or path
            continue
        if not line.startswith("+"):
            continue
        if suspicious(line[1:]):
            _report(path, "+", line[1:])
            hits += 1
    return hits


def _paths(listing):
    for path in listing.splitlines():
        if path and path not in SKIP_PATHS:
            yield path


def scan_tree(commit=None):
    hits = 0
    ref = commit or "HEAD"
    if commit:
        listing = _git("ls-tree", "-r", "--name-only", commit)
    else:
        listing = _git("ls-files")
    for path in _paths(listing):
        blob = _git("show", "%s:%s" % (ref, path))
        if "\0" in blob[:4096]:
            continue                    # binary
        for i, line in enumerate(blob.splitlines(), 1):
            if suspicious(line):
                _report("%s %s" % (ref, path), i, line)
                hits += 1
    return hits


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--tree", action="store_true")
    g.add_argument("--worktree", action="store_true")
    g.add_argument("--commit")
    a = p.parse_args()

    if a.staged:
        hits = scan_staged()
    elif a.tree:
        hits = scan_tree()
    elif a.worktree:
        hits = scan_worktree()
    else:
        hits = scan_tree(a.commit)

    if hits:
        print("")
        print("%d credential-looking assignment(s) found." % hits)
        print("If it is genuinely not a secret, make that obvious in the")
        print("VALUE itself - CHANGEME, placeholder, example - rather than")
        print("in a comment beside it.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
