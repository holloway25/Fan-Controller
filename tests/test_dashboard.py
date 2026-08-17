"""Dashboard wiring: the decisions the browser makes, not the server's.

These are SOURCE assertions against templates/index.html. There is no
JavaScript engine on the deployment host, so nothing here executes the
dashboard - it only checks that the wiring the server's behaviour
depends on is still present.

Be clear about the limit: MOST of these catch a line being DELETED
rather than being wrong. Two exceptions, added later, check the
OPERATORS in the fetch guard - that it compares for difference rather
than equality, and that the two halves are OR'd rather than AND'd -
because those are the two ways that guard has actually been wrong. The
limit stands everywhere else: a line referencing the right names and
using them backwards still passes the rest of this file.

They exist because the alternative for this particular defect was a
test named after the dashboard that never opened the file, which is
worse than no test.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(SRC, "templates", "index.html")


def page():
    return open(PAGE, encoding="utf-8").read()


def squashed():
    """The page with all whitespace removed.

    Token checks should survive re-indenting and line rewrapping, which
    change nothing about behaviour. Matching raw source would make this
    fail on a reformat and pass on a rename, which is backwards.
    """
    return re.sub(r"\s+", "", page())


def guard_condition(html):
    """The condition guarding the history fetch.

    Located by the HIST_BUSY flag rather than by line number, and
    returned as everything between the opening if( and that flag.
    """
    at = html.find("HIST_BUSY)")
    assert at != -1, "could not find the history fetch guard at all"
    start = html.rfind("if(", 0, at)
    assert start != -1, "the guard is not an if()"
    return html[start:at]


def test_the_fetch_guard_watches_the_generation_as_well():
    """The whole point of putting the generation in /api/state.

    The server refuses a cross-generation delta, but only if a request
    is made. A tab holding a revision the replacement has climbed back
    to sends none, so the refusal never fires and the chart stays stale.
    """
    cond = guard_condition(squashed())

    assert "HIST_REV" in cond, "the guard ignores the revision"
    assert "HIST_GEN" in cond, (
        "the guard decides using only the revision, so a tab that slept "
        "through a restart never asks and never learns the trace is from "
        "a process that no longer exists")
    return "guard watches both"


def test_the_fetch_guard_compares_for_difference_not_equality():
    """The inversion the test above cannot see.

    That one proves both names are MENTIONED. A guard that mentions
    them and compares with === refetches only while the client is
    already up to date, and stops the moment it falls behind - exactly
    backwards, and green against every other assertion in this file.

    Checks the OPERATOR rather than the operand order, so rewriting
    HIST_REV!==s.history_rev the other way round does not fail a test
    about something else.
    """
    cond = guard_condition(squashed())
    assert "===" not in cond, (
        "the fetch guard compares for equality, so it refetches while "
        "the trace is current and stops when it goes stale")
    assert cond.count("!==") == 2, (
        "expected two inequality comparisons, revision and generation, "
        "found %d" % cond.count("!=="))
    return "two !== and no ==="


def test_the_revision_and_generation_are_ored_not_anded():
    """Either one differing has to be enough on its own.

    Requiring BOTH is the 3e0aca4 defect reached by a different route:
    a tab holding a revision the replacement has climbed back to
    matches on the revision, so an AND never fires, nothing refetches,
    and the next delta is appended to a trace from a process that no
    longer exists.
    """
    cond = guard_condition(squashed())
    assert "||" in cond, (
        "the revision and generation checks are not OR'd, so one of "
        "them differing on its own does not trigger a fetch")
    return "revision OR generation"


def test_the_delta_request_sends_both_since_and_gen():
    """A delta the server cannot validate is one it will refuse."""
    html = squashed()
    assert "since=" in html, "the client never asks for a delta at all"
    assert "gen=" in html, (
        "the client asks for deltas without saying which process issued "
        "the revision, so every request falls back to a full copy")
    return "since and gen both sent"


def test_the_unsaved_fault_warning_is_rendered():
    """It is in /api/state and was invisible for months."""
    html = squashed()
    assert 'id="unsavedmsg"' in html, "no element to show it in"
    assert "s.fault_unsaved" in html, (
        "nothing reads the flag, so a fault that cannot reach disk shows "
        "only in the activity log")
    return "element present and driven from the flag"


def test_the_history_fetch_stores_what_the_response_carried():
    """Storing the revision seen in /api/state instead would lose any
    change that landed while the fetch was in flight."""
    html = squashed()
    assert "HIST_REV=h.history_rev" in html, (
        "the revision is not taken from the history response")
    assert "HIST_GEN=h.history_generation" in html, (
        "the generation is not taken from the history response")
    return "both taken from the response"


# ------------------------------------------------- the activity log column

def test_the_log_colours_on_the_code_series_not_the_wording():
    """The rules matched "^Draw fault" and "^Fan " until 2026-08-16.

    Both stopped working the moment log lines gained an FC- prefix, and
    nothing caught it: no test reads these strings, and the only place
    the breakage shows is a screen. Matching the SERIES means a new code
    inherits the right colour without this list being edited.
    """
    html = squashed()
    assert "FC-(" in html or "FC-\\d" in html, (
        "the log no longer looks at the fault code at all")
    for series in ('code==="0"', 'code==="1"', 'code==="2"', 'code==="3"'):
        assert series in html, "no colour rule for series %s" % series
    return "all four series coloured"


def test_the_code_prefix_is_stripped_before_the_text_rules():
    """An uncoded line and a coded one must take the same branch."""
    html = squashed()
    assert 'replace(/^FC-\\d+\\s+/,"")' in html, (
        "the code prefix is not stripped before the text rules run")
    return "prefix stripped"


def test_a_folded_mode_line_is_still_coloured():
    """Folding produces "Mode: Manual - fan off". The rule used to
    compare the whole string, so folded lines lost their colour."""
    # squashed() removes ALL whitespace, so the needles must too - the
    # spaced version of the negative assertion below passed vacuously
    # when first written, which is the failure mode of any "not in".
    html = squashed()
    assert 'm==="Mode:Manual"' not in html, (
        "still comparing the whole string; a folded mode line will not "
        "match")
    assert '/^Mode:Manual/.test(m)' in html, "no prefix match for Manual"
    assert '/^Mode:Auto/.test(m)' in html, "no prefix match for Auto"
    return "folded mode lines match"


def test_the_timestamp_is_short_but_the_full_one_is_reachable():
    """The column never wraps, so every character it spends is taken
    from the message. The precision lives in events.json."""
    html = squashed()
    # The year left the column entirely: today shows a time only, older
    # entries a day and month. The full value is one tap away.
    assert "sameDay=d.toDateString()" in html, (
        "the column still spends width on today's date")
    # The VISIBLE assignment only. Seconds belong in the datetime
    # attribute - a blanket "no getSeconds anywhere" assertion failed
    # the moment the machine-readable value was added, which is the
    # right result for the wrong reason.
    i = html.index("when.textContent=")
    shown = html[i:html.index(";", i)]
    assert "getSeconds" not in shown, (
        "seconds are still shown in the log column: %s" % shown)
    assert "when.dateTime=" in html, "no machine-readable timestamp"
    assert "getSeconds" in html[html.index("when.dateTime="):], (
        "the datetime attribute drops the seconds")
    assert "when.title=" in html, (
        "the full timestamp is not reachable on hover")
    return "short column, full value in datetime"


def test_the_timestamp_is_reachable_without_a_hover():
    """A tooltip is nothing on a touch screen."""
    html = squashed()
    assert 'createElement("time")' in html, (
        "the timestamp is not a <time> element")
    assert 'setAttribute("aria-label"' in html, (
        "no accessible label on the timestamp")
    return "readable by touch and by screen reader"


def test_the_mode_buttons_announce_which_is_selected():
    """Selection was carried by a colour alone, so a screen reader
    announced three identical buttons."""
    html = squashed()
    assert 'setAttribute("aria-pressed"' in html, "no aria-pressed on modes"
    return "selection is announced"


def test_a_checkbox_does_not_stretch_across_the_row():
    """A grid item stretches to fill its column by default, so
    width:auto alone still gave a checkbox the full screen width."""
    html = squashed()
    assert "justify-self:start" in html, (
        "checkboxes will stretch to the full row width on a phone")
    return "checkbox sits with its label"


def test_the_log_colours_reach_the_time_element():
    """The timestamp stopped being a span. A colour rule listing only
    span would leave it grey on every coloured row."""
    html = squashed()
    for cls in ("fault", "ok", "m-auto", "m-manual", "m-on"):
        assert ".logdiv.%stime" % cls in html, (
            "the %s colour does not reach the timestamp" % cls)
    return "timestamps take the row colour"


# ------------------------------------------------------ phone compatibility

def test_inputs_are_16px_on_a_narrow_screen():
    """Mobile Safari zooms the page in when a focused field is under
    16px. Tapping a threshold jumped the layout and left you pinching
    back out."""
    html = squashed()
    assert "font-size:16px" in html, (
        "no 16px rule; focusing an input will zoom the page on iOS")
    return "no zoom on focus"


def test_the_page_does_not_wait_on_google_to_render():
    """This is a LAN device. It must render on a LAN alone.

    As a plain stylesheet the font link held the whole page until Google
    answered, so a weak signal or an internet outage meant a blank
    dashboard rather than a plain-looking one.
    """
    html = squashed()
    assert 'fonts.googleapis.com' not in html or 'media="print"' in html, (
        "the font stylesheet is render-blocking again")
    return "fonts never block the fan state"


def test_the_font_stack_falls_back_to_system_fonts():
    """If the webfont never arrives the page must still be readable."""
    html = squashed()
    assert "system-ui" in html, "no system fallback for the sans stack"
    assert "ui-monospace" in html or "Menlo" in html, (
        "no system fallback for the mono stack")
    return "readable without the webfont"


def test_the_viewport_height_uses_dvh():
    """100vh measures the area behind mobile Safari's toolbar, so the
    page was always taller than the screen and rubber-banded."""
    html = squashed()
    assert "100dvh" in html, "no dvh rule"
    assert "100vh" in html, "the vh fallback was removed"
    return "dvh with a vh fallback"


def test_a_long_log_line_can_wrap():
    """A flex item will not shrink below its content without
    min-width:0, and a redacted exception is longer than a phone is
    wide."""
    html = squashed()
    assert "min-width:0" in html, (
        "log text has no min-width:0; long lines will widen the row")
    assert 'txtEl.className="txt"' in html, (
        "the message is not in an element the CSS can target")
    return "long lines wrap"


def test_the_manifest_is_linked():
    """Without it the dashboard opens in a tab with a URL bar rather
    than standalone when added to a home screen."""
    html = squashed()
    assert "manifest.webmanifest" in html, "no manifest link"
    return "installs standalone"


def test_the_timestamp_expands_on_tap():
    """title needs a mouse and datetime is for machines, so the full
    moment was unreachable to an ordinary sighted phone user."""
    html = squashed()
    assert 'when.tabIndex=0' in html, "the timestamp cannot be focused"
    assert 'setAttribute("role","button")' in html, "no button semantics"
    assert 'addEventListener("click",toggle)' in html, "no tap handler"
    assert 'ev.key==="Enter"' in html, "not reachable from a keyboard"
    assert 'aria-expanded' in html, "state is not announced"
    return "tap or Enter expands it"


def test_the_expanded_timestamp_is_allowed_to_wrap():
    """The long form is wider than the column it sits in."""
    html = squashed()
    assert 'aria-expanded=true]{white-space:normal}' in html, (
        "expanded timestamps will be clipped to one line")
    return "expanded form can wrap"


def test_the_tap_handler_is_bound_once():
    """A patch applied twice bound the listener twice - the same tap
    would have toggled it back and forth in one gesture."""
    html = squashed()
    assert html.count("constshortTxt=when.textContent") == 1, (
        "the tap handler is bound %d times"
        % html.count("constshortTxt=when.textContent"))
    return "bound once"


# ------------------------------------------------------- the chart headers

def test_every_chart_names_itself_in_its_own_colour():
    """CO2 was captioned grey while its trace was white, so the label
    read as belonging to nothing in particular."""
    html = squashed()
    i = html.index('txt("CO2PPM"')
    head = html[i:i + 120]
    assert 'fill:"#e8edf3"' in head, "the CO2 label is not the trace colour"
    return "labels match their traces"


def test_every_chart_is_named():
    """Casing is a matter of taste and was changed back on 2026-08-17 -
    the mini rows read as sentences again. What is worth pinning is
    that each chart still says what it is, and that the CO2 header did
    not quietly lose its name when the other two were reverted."""
    html = squashed()
    for name in ('"CO2PPM"', 'Temperature', 'Humidity'):
        assert name in html, "%s is missing from the charts" % name
    return "all three named"


def test_every_chart_shows_a_labelled_range():
    """A bare dash range reads as one number until you look twice, and
    the CO2 chart showed no range at all."""
    html = squashed()
    assert html.count('"Low"+') == 2, (
        'expected a labelled range on the CO2 chart and the mini rows, '
        'found %d' % html.count('"Low"+'))
    assert '"\\u2013"' not in html, "a dash range is still being drawn"
    return "low and high, worded"


def test_the_range_is_the_observed_values_not_the_padded_scale():
    """lo and hi are stretched to keep the threshold lines on screen.
    Reporting those as the range would claim readings that never
    happened - a fan-on line at 70% would have the humidity row
    claiming a 70% high on a day it never passed 55."""
    html = squashed()
    assert html.count("constdlo=Math.min(...vs),dhi=Math.max(...vs)") == 2, (
        "the range is not derived separately from the padded scale")
    return "observed, not padded"


def test_the_chart_labels_scale_up_on_a_narrow_screen():
    """The SVG scales to its card, so 12-unit text landed near 5px on a
    390px screen. Raising the font sizes would collide the labels at
    desktop width; shrinking the coordinate space does not, because
    every distance in the chart is derived from W and H."""
    html = squashed()
    assert "functionfitViewBox" in html, "no responsive viewBox"
    assert 'matchMedia("(max-width:640px)")' in html, (
        "the narrow box is not tied to the phone breakpoint")
    assert "fitViewBox(svg,700,216,345,170)" in html, (
        "the CO2 chart does not narrow")
    assert "fitViewBox(svg,700,159,345,150)" in html, (
        "the mini charts do not narrow")
    return "narrower box, larger labels"


def test_the_narrow_box_is_taller_and_keeps_its_shape():
    """Two properties, and the first is easy to break while fixing the
    second.

    TALLER THAN THE DESKTOP BOX: a phone has vertical room where it has
    none sideways, and the plot would otherwise be a band once the
    labels take a larger share of the width.

    AND THE SAME SHAPE AS THE LAST NARROW BOX: rendered height is
    cssWidth * H/W, so shrinking the width to enlarge the labels makes
    the chart taller on screen unless the height comes down with it.
    The labels grow; the chart stays the size it was.
    """
    import re as _re
    html = squashed()
    calls = _re.findall(r"fitViewBox\(svg,(\d+),(\d+),(\d+),(\d+)\)", html)
    assert calls, "no fitViewBox calls found"
    for ww, wh, nw, nh in calls:
        wide = int(wh) / int(ww)
        narrow = int(nh) / int(nw)
        assert narrow > wide, (
            "%s x %s is not taller than %s x %s" % (nw, nh, ww, wh))
        # AND NOT SO TALL THAT IT LOOKS SQUASHED SIDEWAYS. At 0.63 the
        # CO2 chart was nearly square and the traces read as
        # vertically stretched. The ceiling is what stops a future
        # label enlargement being paid for in height again - narrowing
        # the box to grow the text raises this number unless the height
        # is brought down with it.
        assert narrow < 0.55, (
            "narrow aspect %.3f - the chart is squashed horizontally"
            % narrow)
    return "taller than desktop, not square"


def test_a_manual_external_change_reads_as_ordinary():
    """In Manual, a hand switch is the mode doing its job - it should
    not be coloured as something to look at. In any other mode the same
    line means the controller was contradicted."""
    html = squashed()
    assert '/\\(Manual\\)$/.test(m)?"ok":"m-manual"' in html, (
        "FC-300 does not distinguish Manual from the other modes")
    return "green in Manual, flagged otherwise"


def test_the_page_shows_which_commit_it_is_running():
    """The wiring, end to end - not three strings in the source.

    The first version of this only grepped the HTML, so it would have
    passed with /api/state no longer returning the field and the footer
    left empty. Reported by review of e91cbb0.
    """
    C = harness.load()
    C.VERSION = "abc1234"
    body = C.app.test_client().get("/api/state").get_json()
    assert body.get("version") == "abc1234", (
        "the endpoint does not carry the running version: %r"
        % body.get("version"))

    # And the page puts what it receives into the footer element.
    html = squashed()
    assert 'id="ver"' in html, "no version element to fill"
    assert 've=$("ver")' in html, "the footer is never located"
    assert "ve.textContent=s.version" in html, (
        "the footer is not filled from the state field")
    return "endpoint carries it, page renders it"


def test_the_version_is_always_a_string():
    """A missing field would make the footer read "undefined". The
    fallbacks exist so the page never has to cope with an absent
    value."""
    C = harness.load()
    body = C.app.test_client().get("/api/state").get_json()
    assert isinstance(body.get("version"), str) and body["version"], body.get(
        "version")
    return "never absent, never null"


def test_a_deployment_without_git_still_has_a_version():
    """A tarball copy with no .git is a legitimate install, and the page
    must not break because the corner of it has nothing to say.

    DIR is pointed at a directory that is genuinely not a repository,
    so git really does fail here rather than being stubbed into
    failing."""
    import os
    import shutil
    import tempfile
    C = harness.load()
    real_dir = C.DIR
    tmp = tempfile.mkdtemp()
    try:
        C.DIR = tmp
        # No git, no VERSION file.
        assert C._build_version() == "unknown", C._build_version()
        # A VERSION file is used when git cannot answer.
        with open(os.path.join(tmp, "VERSION"), "w") as f:
            f.write("1.2.3-tarball\n")
        assert C._build_version() == "1.2.3-tarball", C._build_version()
    finally:
        C.DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)
    return "git, then VERSION, then unknown"


def test_a_clean_install_with_a_venv_is_not_marked_modified():
    """The README tells you to create venv/. If it is not ignored, the
    dirty check counts it and every clean checkout reports itself as
    modified.

    THE PROJECT'S REAL .gitignore IS COPIED IN, so removing the rules
    from it fails this. Python's own venv module writes a
    venv/.gitignore containing "*", which hid the problem on the
    machine this was written on - the venv here is made WITHOUT that
    file, the way virtualenv and older Pythons do. Reported by review
    of e38907a.
    """
    import os
    import shutil
    import subprocess
    import tempfile
    C = harness.load()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp()
    real_dir = C.DIR
    try:
        shutil.copy(os.path.join(root, ".gitignore"), tmp)
        run = lambda *a: subprocess.run(a, cwd=tmp, capture_output=True)
        run("git", "init", "-q", ".")
        run("git", "config", "user.email", "t@t")
        run("git", "config", "user.name", "t")
        with open(os.path.join(tmp, "controller.py"), "w") as f:
            f.write("# tracked\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "base")

        C.DIR = tmp
        clean = C._build_version()
        assert clean and not clean.endswith("+"), (
            "a fresh checkout is already marked modified: %r" % clean)

        # A venv WITHOUT the self-ignoring file venv creates.
        os.makedirs(os.path.join(tmp, "venv", "bin"))
        with open(os.path.join(tmp, "venv", "bin", "python"), "w") as f:
            f.write("#!/bin/sh\n")
        after = C._build_version()
        assert after == clean, (
            "creating a venv marked the install modified: %r -> %r"
            % (clean, after))

        # And a real edit still does mark it.
        with open(os.path.join(tmp, "controller.py"), "a") as f:
            f.write("# edited\n")
        assert C._build_version().endswith("+"), (
            "an edited tree is not marked modified")
    finally:
        C.DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)
    return "venv ignored, real edits still counted"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
