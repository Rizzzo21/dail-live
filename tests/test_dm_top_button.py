"""DM thread Back to top button.

Scrolling all the way down a long DM thread had no quick way back up.
The button appears only after scrolling down a meaningful distance,
sits fixed in the corner clear of the jump bar and message content,
and smooth-scrolls to the top on click. It lives outside the
containers renderDM() rewrites, so the auto-refresh never touches it,
and it never takes focus. All fixtures are synthetic; no real DM
thread is ever read (DM thread reads are admin-gated).
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)
HTML = Path("dail/observatory.html").read_text(encoding="utf-8")


def _extract_fn(html, name):
    """Extract one top-level `function name(...) {...}` by brace matching."""
    start = html.index("function %s(" % name)
    i = html.index("{", start)
    depth, j = 0, i
    while True:
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return html[start:j + 1]


def _extract_onclick(html):
    """Extract the dmTopBtn onclick assignment statement by brace matching."""
    start = html.index("document.getElementById('dmTopBtn').onclick")
    i = html.index("{", start)
    depth, j = 0, i
    while True:
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return html[start:j + 2]  # include the trailing semicolon


def _run_node(js):
    node = shutil.which("node")
    assert node, "node required for JS logic tests"
    r = subprocess.run([node, "-"], input=js, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0, f"node test failed: {r.stderr[:800]}"


def test_dm_top_button_served_markers():
    """The served Observatory page carries the Back to top button."""
    r = client.get("/observatory")
    assert r.status_code == 200, r.text
    for marker in ('id="dmTopBtn"', "dmTopShouldShow", "DM_TOP_SHOW",
                   "addEventListener('scroll',dmTopUpdate",
                   "behavior:'smooth'"):
        assert marker in r.text, marker


def test_dm_top_button_inside_thread_panel():
    """The button lives inside the thread panel (hidden with it) but
    outside the containers renderDM() rewrites (dmNav, dmIndex, dmConv)."""
    panel = HTML.index('id="dmConvPanel"')
    btn = HTML.index('id="dmTopBtn"')
    main_end = HTML.index('</main>')
    assert panel < btn < main_end
    # The button is not inside the re-rendered message container.
    conv = HTML.index('id="dmConv"')
    assert not (conv < btn < HTML.index('id="msgbox"'))


def test_dm_top_button_js_parses():
    m = re.search(r"<script>(.*)</script>", HTML, re.S)
    assert m, "no inline script found in observatory.html"
    node = shutil.which("node")
    assert node, "node required for JS syntax check"
    r = subprocess.run([node, "--check", "-"], input=m.group(1),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"observatory.html JS syntax error: {r.stderr[:500]}"


def test_dm_top_button_logic():
    """Show/hide thresholds, click-to-top, and no interference with
    the auto-refresh path, executed in node against synthetic fakes."""
    show_fn = _extract_fn(HTML, "dmTopShouldShow")
    update_fn = _extract_fn(HTML, "dmTopUpdate")
    threshold = re.search(r"var DM_TOP_SHOW=(\d+);", HTML)
    assert threshold, "DM_TOP_SHOW constant missing"
    onclick = _extract_onclick(HTML)
    harness = """
const assert = require('assert');
""" + show_fn + """
// pure threshold: strictly greater than the constant
assert.strictEqual(dmTopShouldShow(0, 600), false);
assert.strictEqual(dmTopShouldShow(600, 600), false);
assert.strictEqual(dmTopShouldShow(601, 600), true);
assert.strictEqual(dmTopShouldShow(5000, 600), true);
// dmTopUpdate: visibility follows scroll position and thread-open state
""" + "var DM_TOP_SHOW=%s;\n" % threshold.group(1) + update_fn + """
let btn = {style: {display: 'none'}};
global.window = {scrollY: 0};
global.document = {getElementById: () => btn};
global.dmAgent = 'agent_x';
window.scrollY = 0; dmTopUpdate();
assert.strictEqual(btn.style.display, 'none', 'hidden at top');
window.scrollY = 600; dmTopUpdate();
assert.strictEqual(btn.style.display, 'none', 'hidden at threshold');
window.scrollY = 601; dmTopUpdate();
assert.strictEqual(btn.style.display, '', 'shown past threshold');
window.scrollY = 5000; dmTopUpdate();
assert.strictEqual(btn.style.display, '', 'shown deep in thread');
global.dmAgent = null; dmTopUpdate();
assert.strictEqual(btn.style.display, 'none', 'hidden with no thread open');
// click: smooth-scrolls to top and blurs, never focuses anything
let scrolled = null, blurred = false;
global.window = {scrollTo: (opts) => { scrolled = opts; }};
btn = {style: {display: ''}, blur: () => { blurred = true; }, onclick: null};
global.document = {getElementById: () => btn};
""" + onclick + """
btn.onclick.call(btn);
assert.deepStrictEqual(scrolled, {top: 0, behavior: 'smooth'});
assert.strictEqual(blurred, true, 'button blurs itself after click');
console.log('dm top button logic OK');
"""
    _run_node(harness)


def test_dm_top_button_untouched_by_refresh():
    """renderDM() and openDM() — the auto-refresh path — never touch
    the button, so a refresh cannot show it, hide it, or move focus."""
    for name in ["renderDM", "openDM"]:
        body = _extract_fn(HTML, name)
        assert "dmTopBtn" not in body, "%s touches dmTopBtn" % name
        assert "dmTopUpdate" not in body, "%s touches dmTopUpdate" % name
