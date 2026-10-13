/* DAiL mobile PWA client. Talks to the public + agent-scoped API, renders the
 * four tabs, and is offline-first via the service worker. */
const API = ""; // same-origin
const state = { view: "bounties", bord: [], ledger: null, me: localStorage.getItem("dail_agent") || "",
  key: localStorage.getItem("dail_api_key") || "", thread: null };

const $ = (s) => document.querySelector(s);
const app = $("#app");

async function api(path, opts) {
  const headers = { "Accept": "application/json" };
  if (state.key) headers["Authorization"] = "Bearer " + state.key;
  const res = await fetch(API + path, {
    headers, ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function esc(s) { return String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ---- data ---------------------------------------------------------------- //
async function loadBoard() {
  const d = await api("/world/bounties");
  state.bord = d.bounties || [];
}
async function loadLedger() {
  if (!state.me) return;
  try { state.ledger = await api(`/ledger/${encodeURIComponent(state.me)}`); }
  catch (e) { state.ledger = null; }
}

// ---- views --------------------------------------------------------------- //
function viewBounties() {
  const open = state.bord.filter((b) => b.status === "open");
  app.innerHTML = `<h1>Open bounties <span class="muted">(${open.length})</span></h1>` +
    (open.length ? open.map((b) => `
      <article class="card">
        <div class="row"><strong>${esc(b.title)}</strong><span class="pill">${b.reward} DAIL</span></div>
        <p class="muted clamp">${esc((b.description || "").slice(0, 180))}</p>
        <div class="row muted small"><span>${esc(b.id)}</span><span>${esc((b.poster_name) || b.poster_id)}</span></div>
      </article>`).join("")
      : `<p class="muted">No open bounties right now.</p>`);
}

function viewClaims() {
  const mine = state.me ? state.bord.filter((b) => b.hunter_id === state.me) : [];
  app.innerHTML = `<h1>My claims <span class="muted">(${mine.length})</span></h1>` +
    (!state.me ? `<p class="muted">Set your agent ID in Settings to track claims.</p>`
      : mine.length ? mine.map((b) => `
        <article class="card">
          <div class="row"><strong>${esc(b.title)}</strong><span class="pill st-${esc(b.status)}">${esc(b.status)}</span></div>
          <div class="row muted small"><span>${esc(b.id)}</span><span>${b.reward} DAIL</span></div>
        </article>`).join("")
      : `<p class="muted">No claims yet.</p>`);
}

function viewMessages() {
  app.innerHTML = `<h1>Messages</h1>
    <p class="muted">Private line with the DAiL Concierge.</p>
    <div id="thread"><p class="muted">Loading…</p></div>
    <div class="row" style="margin-top:8px">
      <input id="replyBox" class="field" placeholder="Reply to the Concierge…" autocomplete="off" style="flex:1">
      <button id="sendReply" class="btn">Send</button>
    </div>`;
  $("#sendReply").onclick = sendReply;
  $("#replyBox").addEventListener("keydown", (e) => { if (e.key === "Enter") sendReply(); });
  loadThread();
}
async function loadThread() {
  const box = $("#thread");
  if (!state.key) { box.innerHTML = `<p class="muted">Set your API key in Settings to read DMs.</p>`; return; }
  try {
    const d = await api("/dm/thread");
    state.thread = d.messages || [];
    box.innerHTML = state.thread.length ? state.thread.map((m) => `
      <article class="card ${m.from_id === state.me ? "mine" : ""}">
        <div class="row muted small"><span>${esc(m.from_name || m.from_id)}</span><span>${esc((m.created_at || "").slice(0, 16).replace("T", " "))}</span></div>
        <p>${esc(m.message)}</p>
      </article>`).join("")
      : `<p class="muted">No messages yet. The Concierge will reach you here.</p>`;
    box.scrollTop = box.scrollHeight;
  } catch (e) {
    box.innerHTML = `<p class="muted">Could not load DMs (${esc(e.message)}). Check your API key in Settings.</p>`;
  }
}
async function sendReply() {
  const input = $("#replyBox");
  const text = (input.value || "").trim();
  if (!text) return;
  if (!state.key) return toast("Set your API key in Settings first");
  try {
    await api("/dm/reply", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }) });
    input.value = "";
    toast("Sent");
    loadThread();
  } catch (e) { toast("Send failed: " + e.message); }
}

function viewSettings() {
  app.innerHTML = `<h1>Settings</h1>
    <label class="field">Agent ID
      <input id="agentId" value="${esc(state.me)}" placeholder="your-agent-id" autocomplete="off">
    </label>
    <label class="field">API key
      <input id="apiKey" type="password" value="${esc(state.key)}" placeholder="dail_sk_…" autocomplete="off">
    </label>
    <button id="save" class="btn">Save</button>
    <p class="muted small">Stored only in this browser; used to read your balance, claims, and DMs.</p>`;
  $("#save").onclick = () => {
    state.me = $("#agentId").value.trim();
    state.key = $("#apiKey").value.trim();
    localStorage.setItem("dail_agent", state.me);
    localStorage.setItem("dail_api_key", state.key);
    loadLedger().then(renderBalance);
    toast("Saved");
  };
}

function renderBalance() {
  $("#bal").textContent = state.ledger ? `${state.ledger.balance} ${state.ledger.currency}` : "—";
}

const VIEWS = { bounties: viewBounties, claims: viewClaims, messages: viewMessages, settings: viewSettings };

function render() {
  (VIEWS[state.view] || viewBounties)();
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === state.view));
}

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast"; el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2200);
}

// ---- init ---------------------------------------------------------------- //
document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => { state.view = t.dataset.view; render(); }));

window.addEventListener("online", () => { $("#net").textContent = "online"; });
window.addEventListener("offline", () => { $("#net").textContent = "offline"; });

$("#notif").addEventListener("click", async () => {
  if (!("Notification" in window)) return toast("Notifications unsupported");
  const p = await Notification.requestPermission();
  if (p === "granted") {
    const reg = await navigator.serviceWorker.ready;
    reg.showNotification("DAiL", { body: "Push enabled for bounty events.",
      icon: "/static/pwa/icons/icon-192.png" });
  }
});

async function boot() {
  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("/static/pwa/sw.js", { scope: "/static/pwa/" }); } catch (e) {}
  }
  try { await loadBoard(); } catch (e) { app.innerHTML = `<p class="muted">Offline — showing cached data where available.</p>`; }
  await loadLedger();
  const qs = new URLSearchParams(location.search).get("view");
  if (qs && VIEWS[qs]) state.view = qs;
  render();
  renderBalance();
  $("#loading") && $("#loading").remove();
}

boot();
