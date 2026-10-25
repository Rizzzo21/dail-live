"""DM thread navigation: anchors, message index, Latest / First unread jumps,
most-recent-50 window with Load earlier.

The Observatory DM tab used to render a whole thread as one unbroken list,
so reaching the next message meant scrolling the entire history. These tests
cover the navigation layer only. All fixtures are synthetic; no real DM
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


def _run_node(js):
    node = shutil.which("node")
    assert node, "node required for JS logic tests"
    r = subprocess.run([node, "-"], input=js, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0, f"node test failed: {r.stderr[:800]}"


def _fixture(n):
    out = []
    for i in range(n):
        out.append({
            "from_id": "dail_concierge" if i % 2 == 0 else "agent_x",
            "from_name": "DAiL Concierge" if i % 2 == 0 else "Agent X",
            "created_at": "2026-10-24T%02d:00:00+00:00" % (i % 24),
            "message": "message number %d " % i + "y" * 80,
        })
    return out


def test_dm_nav_served_markers():
    """The served Observatory page carries the navigation controls."""
    r = client.get("/observatory")
    assert r.status_code == 200, r.text
    for marker in ('id="dmNav"', 'id="dmIndex"', 'id="dmLoadEarlier"',
                   "data-unread", "DM_PAGE", "dmRenderThread",
                   "First unread", "Latest"):
        assert marker in r.text, marker


def test_dm_nav_js_parses():
    m = re.search(r"<script>(.*)</script>", HTML, re.S)
    assert m, "no inline script found in observatory.html"
    node = shutil.which("node")
    assert node, "node required for JS syntax check"
    r = subprocess.run([node, "--check", "-"], input=m.group(1),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"observatory.html JS syntax error: {r.stderr[:500]}"


def test_dm_nav_logic():
    """Pure navigation helpers, executed in node against a synthetic thread."""
    fns = "\n".join(_extract_fn(HTML, n) for n in
                     ["dmAnchor", "dmPreview", "dmFirstUnreadIdx",
                      "dmWindowStart", "dmRenderThread"])
    harness = """
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function ago(iso){return iso||'–'}
""" + fns + """
const assert = require('assert');
// anchors
assert.strictEqual(dmAnchor(3), 'dm-msg-3');
// preview: 60 chars + ellipsis, short text untouched
assert.strictEqual(dmPreview('x'.repeat(61)), 'x'.repeat(60) + '…');
assert.strictEqual(dmPreview('short'), 'short');
// first unread: unread messages are the newest ones
assert.strictEqual(dmFirstUnreadIdx(10, 3), 7);
assert.strictEqual(dmFirstUnreadIdx(10, 0), -1);
assert.strictEqual(dmFirstUnreadIdx(5, 9), 0);
// window: most recent page, widened to include the first unread message
assert.strictEqual(dmWindowStart(120, -1, 50), 70);
assert.strictEqual(dmWindowStart(120, 10, 50), 10);
assert.strictEqual(dmWindowStart(30, -1, 50), 0);
// render: 120-message fixture, 3 unread, window 70..119
const msgs = [];
for (let i = 0; i < 120; i++) {
  msgs.push({from_id: i % 2 ? 'agent_x' : 'dail_concierge',
             from_name: i % 2 ? 'Agent X' : 'DAiL Concierge',
             created_at: '2026-10-24T10:00:00+00:00',
             message: 'message number ' + i + ' ' + 'y'.repeat(80)});
}
let r = dmRenderThread(msgs, 70, 117);
assert.ok(r.nav.includes('href="#dm-msg-119"'), 'Latest jump');
assert.ok(r.nav.includes('href="#dm-msg-117"'), 'First unread jump');
assert.strictEqual(r.showEarlier, true);
assert.strictEqual((r.body.match(/id="dm-msg-/g) || []).length, 50, 'page size');
// every index link resolves to a rendered anchor
const ids = new Set([...r.body.matchAll(/id="(dm-msg-\\d+)"/g)].map(m => m[1]));
const links = [...r.index.matchAll(/href="#(dm-msg-\\d+)"/g)].map(m => m[1]);
assert.strictEqual(links.length, 50);
for (const l of links) assert.ok(ids.has(l), 'index link without anchor: ' + l);
// index previews are truncated, bodies are not
assert.ok(r.index.includes('…'), 'index preview truncation');
assert.ok(r.body.includes('y'.repeat(80)), 'full body kept');
// full window, no earlier control
r = dmRenderThread(msgs, 0, -1);
assert.strictEqual(r.showEarlier, false);
assert.strictEqual((r.body.match(/id="dm-msg-/g) || []).length, 120);
// empty thread
r = dmRenderThread([], 0, -1);
assert.strictEqual(r.nav, '');
assert.strictEqual(r.index, '');
assert.ok(r.body.includes('No messages yet'));
// escaping in index and body
r = dmRenderThread([{from_id: 'agent_x', from_name: 'Agent X',
                     created_at: '2026-10-24T10:00:00+00:00',
                     message: '<b>hi</b>'}], 0, -1);
assert.ok(r.body.includes('&lt;b&gt;hi&lt;/b&gt;'));
assert.ok(r.index.includes('&lt;b&gt;hi&lt;/b&gt;'));
console.log('dm nav logic OK');
"""
    _run_node(harness)
