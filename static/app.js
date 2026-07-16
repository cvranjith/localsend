// ── state ─────────────────────────────────────────────────────────────────────
let config = {};
let partners = [];
let stagedFiles = [];     // {file_id, name, path, size}
let activeTransfers = {}; // key: `${transfer_id}:${filename}` → DOM element
let outboxBarMap = {};    // `${outbox_transfer_id}:${filename}` → original bar key
let toastStore = {};      // toast_id → {saved_path, text_preview, ...}
let logPaths = {};        // log_entry_id → saved_path

// ── api helpers ───────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const d = await r.json(); msg = d.detail || JSON.stringify(d); }
    catch { msg = await r.text().catch(() => r.statusText); }
    throw new Error(msg);
  }
  return r.json();
}
const GET  = (p)    => api("GET",    p);
const POST = (p, b) => api("POST",   p, b);
const PUT  = (p, b) => api("PUT",    p, b);
const DEL  = (p)    => api("DELETE", p);

// ── init ──────────────────────────────────────────────────────────────────────
async function init() {
  try {
    [config, partners] = await Promise.all([GET("/api/config"), GET("/api/partners")]);
    renderConfig();
    renderPartners();
    const log = await GET("/api/log");
    renderLog(log);
    connectSSE();
    setupDropzone();
    startHeartbeat();
    startStatusRepaint();
  } catch (e) {
    console.error("Init failed", e);
  }
}

// A "client" pings its partners on its own configured schedule; a "server"
// never pings out at all — it just waits to be contacted.
let heartbeatTimer = null;
function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (config.role !== "client") return;
  pingAllPartners();
  heartbeatTimer = setInterval(pingAllPartners, Math.max(10, config.ping_frequency_sec || 60) * 1000);
}

// A server's clients can go stale purely from time passing (a heartbeat window
// lapsing) with no event to push, so it needs to repaint on a timer to catch
// that. A client doesn't: its own heartbeat cadence and live SSE pushes already
// keep it fresh, so skip this there rather than polling redundantly.
let statusRepaintTimer = null;
function startStatusRepaint() {
  if (statusRepaintTimer) clearInterval(statusRepaintTimer);
  if (config.role !== "server") return;
  statusRepaintTimer = setInterval(refreshPartnerStatus, 30000);
}

async function pingAllPartners() {
  if (!partners.length) return;
  try { await GET("/api/partners/ping"); } catch {}
  refreshPartnerStatus();
}

async function refreshPartnerStatus() {
  try {
    partners = await GET("/api/partners");
    renderPartners();
  } catch {}
}

// ── config / header ───────────────────────────────────────────────────────────
function renderConfig() {
  document.getElementById("device-badge").textContent = config.device_name || "unknown";
  const addr = config.local_ip ? `${config.local_ip}:${config.port}` : `port ${config.port}`;
  const el = document.getElementById("addr-badge");
  el.textContent = addr;
  el.title = "Click to copy";
  el.style.cursor = "pointer";
  el.onclick = () => {
    navigator.clipboard.writeText(addr).then(() => {
      el.textContent = "✓ copied!";
      setTimeout(() => { el.textContent = addr; }, 1500);
    });
  };
  const roleEl = document.getElementById("role-badge");
  roleEl.textContent = config.role === "client" ? `client · every ${config.ping_frequency_sec}s` : "server";
}

function openSettings() {
  document.getElementById("cfg-name").value = config.device_name || "";
  document.getElementById("cfg-dir").value  = config.receive_dir || "";
  document.getElementById("cfg-role").value = config.role || "server";
  document.getElementById("cfg-freq").value = config.ping_frequency_sec || 60;
  document.getElementById("cfg-longpoll").value = config.long_poll_timeout_sec || 20;
  onRoleChange();
  openModal("settings-modal");
}

function onRoleChange() {
  const isClient = document.getElementById("cfg-role").value === "client";
  document.getElementById("cfg-freq-row").style.display = isClient ? "" : "none";
  document.getElementById("cfg-longpoll-row").style.display = isClient ? "" : "none";
}

async function saveSettings() {
  try {
    config = { ...config, ...await PUT("/api/config", {
      device_name: document.getElementById("cfg-name").value.trim(),
      receive_dir:  document.getElementById("cfg-dir").value.trim(),
      role: document.getElementById("cfg-role").value,
      ping_frequency_sec: Math.max(10, parseInt(document.getElementById("cfg-freq").value) || 60),
      long_poll_timeout_sec: Math.max(10, parseInt(document.getElementById("cfg-longpoll").value) || 20),
    }) };
    renderConfig();
    renderPartners();
    closeModal("settings-modal");
    startHeartbeat();
    startStatusRepaint();
  } catch (e) { alert("Save failed: " + e.message); }
}

// ── partners ──────────────────────────────────────────────────────────────────
function renderPartners() {
  const el = document.getElementById("partners-list");
  if (!partners.length) {
    el.innerHTML = '<div class="empty">No partners yet — click + Add</div>';
    return;
  }
  el.innerHTML = partners.map(partnerHTML).join("");
}

function partnerHTML(p) {
  const dot = dotFor(p);
  const route = p.route || "local";

  // Fixed at the time the partner was added — never flips with a transient
  // reachability blip (that's what the status dot is for).
  const mode = p.mode || (p.reachable ? "server" : "client");
  const modeBadge = mode === "client"
    ? `<span class="mode-badge mode-client">client</span>`
    : `<span class="mode-badge mode-server">server</span>`;

  const syncTip = route === "cloud"
    ? "Click to sync via the cloud route"
    : route === "auto"
    ? "Click to sync: ping direct, falling back to cloud automatically if it's down"
    : "Click to sync: ping + send staged files + check for incoming";

  return `
  <div class="partner-item" id="partner-${p.id}" onclick="syncPartner('${p.id}')" title="${syncTip}">
    <div class="status-dot ${dot.cls}" id="dot-${p.id}" title="${dot.tip}">${dot.glyph}</div>
    <div class="partner-info">
      <div class="partner-name">${esc(p.name)} ${modeBadge}</div>
      <div class="partner-addr">${esc(p.ip)}:${p.port}</div>
    </div>
    <button class="btn-menu" onclick="event.stopPropagation();openPartnerEdit('${p.id}')" title="Edit partner">&#8942;</button>
  </div>`;
}

// The dot alone communicates connection state — no separate route/scan text
// badges. Green = direct is up. A cloud glyph = route is "cloud" (always),
// or route is "auto" and direct isn't reachable right now (about to fall
// back). Red = direct is down and there's no cloud fallback opted in (route
// "local"). Grey = never contacted yet.
function dotFor(p) {
  const route = p.route || "local";
  if (route === "cloud") {
    return { cls: "cloud", tip: "cloud mode — click syncs via the cloud route", glyph: "☁" };
  }
  if (p.status === "green") {
    return { cls: "online", tip: "online", glyph: "" };
  }
  if (p.status === "red" && route === "auto") {
    return { cls: "cloud", tip: "direct isn't reachable — will use the cloud fallback", glyph: "☁" };
  }
  if (p.status === "red") {
    return { cls: "offline", tip: "not responding", glyph: "" };
  }
  return { cls: "", tip: "waiting for contact", glyph: "" };
}

function openAddPartner() {
  document.getElementById("ap-ip").value     = "";
  document.getElementById("ap-port").value   = config.port || "8765";
  document.getElementById("ap-error").textContent  = "";
  document.getElementById("ap-status").textContent = "";
  openModal("add-partner-modal");
  setTimeout(() => document.getElementById("ap-ip").focus(), 50);
}

async function submitAddPartner() {
  const ip   = document.getElementById("ap-ip").value.trim();
  const port = parseInt(document.getElementById("ap-port").value) || 8765;
  const errEl  = document.getElementById("ap-error");
  const statEl = document.getElementById("ap-status");
  const btn    = document.getElementById("ap-btn");

  if (!ip) { errEl.textContent = "IP address is required"; return; }

  btn.disabled = true;
  btn.textContent = "Saving…";
  errEl.textContent  = "";
  statEl.textContent = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\*$/.test(ip)
    ? "Broadcasting for it on that port…"
    : "Trying to reach partner…";

  try {
    const p = await POST("/api/partners", { ip, port });
    partners.push(p);
    renderPartners();
    closeModal("add-partner-modal");
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Save Partner";
    statEl.textContent = "";
  }
}

// ── edit partner (IP/port/route, plus remove) ─────────────────────────────────
let editingPartnerId = null;

function openPartnerEdit(id) {
  const p = partners.find(x => x.id === id);
  if (!p) return;
  editingPartnerId = id;
  document.getElementById("ep-name").textContent = p.name;
  // Show the wildcard form when scanning is on, so the field reflects intent
  // (roam within this /24) rather than just whatever address was last resolved.
  document.getElementById("ep-ip").value = p.allow_scan
    ? p.ip.split(".").slice(0, 3).join(".") + ".*"
    : p.ip;
  document.getElementById("ep-port").value = p.port;
  document.getElementById("ep-route").value = p.route || "local";
  document.getElementById("ep-error").textContent = "";
  openModal("edit-partner-modal");
}

async function saveEditPartner() {
  if (!editingPartnerId) return;
  const ip = document.getElementById("ep-ip").value.trim();
  const port = parseInt(document.getElementById("ep-port").value) || 0;
  const route = document.getElementById("ep-route").value;
  const errEl = document.getElementById("ep-error");
  if (!ip || !port) { errEl.textContent = "IP address and port are required"; return; }

  try {
    const updated = await PUT(`/api/partners/${editingPartnerId}`, { ip, port, route });
    const p = partners.find(x => x.id === editingPartnerId);
    if (p) Object.assign(p, updated);
    renderPartners();
    closeModal("edit-partner-modal");
  } catch (e) {
    errEl.textContent = e.message;
  }
}

async function removePartnerFromEdit() {
  if (!editingPartnerId) return;
  if (!confirm("Remove this partner?")) return;
  await DEL(`/api/partners/${editingPartnerId}`);
  partners = partners.filter(p => p.id !== editingPartnerId);
  renderPartners();
  closeModal("edit-partner-modal");
  editingPartnerId = null;
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    let msg; try { msg = JSON.parse(e.data); } catch { return; }
    const { type, data } = msg;
    if (type === "ping") return;

    if      (type === "partners_update") { partners = data; renderPartners(); }
    else if (type === "config_update")   { config = data; renderConfig(); }

    else if (type === "partner_active") {
      // Live push the instant either side registers contact — no waiting on a timer.
      const p = partners.find(x => x.id === data.id);
      if (p) p.status = data.status;
      const dotEl = document.querySelector(`#partner-${data.id} .status-dot`);
      if (dotEl && p) {
        const d = dotFor(p);
        dotEl.className = "status-dot " + d.cls;
        dotEl.title = d.tip;
        dotEl.textContent = d.glyph;
      }
    }

    else if (type === "status") {
      const sb = document.getElementById("statusbar");
      if (data.receiving) {
        document.getElementById("status-text").textContent = `Receiving from ${data.partner}…`;
        sb.classList.add("show");
      } else {
        sb.classList.remove("show");
      }
    }

    // ── send side ──
    else if (type === "send_start") {
      data.files.forEach(f => addTransferBar(`${data.transfer_id}:${f.name}`, "send", f.name, data.partner_name, 0, data.transfer_id));
    }
    else if (type === "send_progress") {
      const key = outboxBarMap[`${data.transfer_id}:${data.filename}`] || `${data.transfer_id}:${data.filename}`;
      updateTransferBar(key, data.percent);
    }
    else if (type === "send_complete") {
      const resolvedKey = outboxBarMap[`${data.transfer_id}:${data.filename}`] || `${data.transfer_id}:${data.filename}`;
      completeTransferBar(resolvedKey, "sent ✓");
      setTimeout(() => {
        removeTransferBar(resolvedKey);
        delete outboxBarMap[`${data.transfer_id}:${data.filename}`];
      }, 3000);
    }
    else if (type === "send_queued") {
      const barKey = `${data.transfer_id}:${data.filename}`;
      if (data.outbox_id) {
        outboxBarMap[`${data.outbox_id}:${data.filename}`] = barKey;
      }
      updateTransferBarLabel(barKey, "queued — partner will pull");
    }
    else if (type === "send_error") {
      const key = `${data.transfer_id}:${data.filename}`;
      errorTransferBar(key, data.error);
      setTimeout(() => removeTransferBar(key), 6000);
    }
    else if (type === "send_cancelled") {
      const key = `${data.transfer_id}:${data.filename}`;
      errorTransferBar(key, "cancelled");
      setTimeout(() => removeTransferBar(key), 3000);
    }

    // ── receive side ──
    else if (type === "receive_start") {
      data.files.forEach(f => addTransferBar(`${data.transfer_id}:${f.name}`, "recv", f.name, data.partner_name, 0, data.transfer_id));
    }
    else if (type === "receive_progress") {
      updateTransferBar(`${data.transfer_id}:${data.filename}`, data.percent);
    }
    else if (type === "receive_complete") {
      const key = `${data.transfer_id}:${data.filename}`;
      completeTransferBar(key, `saved as ${data.saved_as}`);
      setTimeout(() => removeTransferBar(key), 3000);
      showToast(data);
    }
    else if (type === "receive_error") {
      const key = `${data.transfer_id}:${data.filename}`;
      errorTransferBar(key, data.error);
      setTimeout(() => removeTransferBar(key), 6000);
    }

    else if (type === "log_entry") { prependLogEntry(data); }
  };

  es.onerror = () => { setTimeout(connectSSE, 3000); es.close(); };
}

// ── drop zone / staging ───────────────────────────────────────────────────────
function setupDropzone() {
  const dz = document.getElementById("dropzone");
  dz.addEventListener("dragover",  e => { e.preventDefault(); dz.classList.add("hover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("hover"));
  dz.addEventListener("drop", e => { e.preventDefault(); dz.classList.remove("hover"); uploadFiles(e.dataTransfer.files); });
}

function handleFileInput(input) {
  uploadFiles(input.files);
  input.value = "";
}

async function uploadFiles(fileList) {
  if (!fileList?.length) return;
  const fd = new FormData();
  for (const f of fileList) fd.append("files", f);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    if (!r.ok) throw new Error(await r.text());
    const uploaded = await r.json();
    uploaded.forEach(f => stagedFiles.push(f));
    renderStaged();
  } catch (e) { alert("Upload failed: " + e.message); }
}

function renderStaged() {
  const list = document.getElementById("staged-list");
  const hint = document.getElementById("send-hint");
  if (!stagedFiles.length) { list.innerHTML = ""; hint.style.display = "none"; return; }
  list.innerHTML = stagedFiles.map((f, i) => `
    <div class="staged-file">
      <span class="fname" title="${esc(f.name)}">${esc(f.name)}</span>
      ${f.origin === "browsed" ? '<span class="mode-badge mode-server" title="Sent straight from disk on this machine — not uploaded">server</span>' : ""}
      <span class="fsize">${fmtSize(f.size)}</span>
      <button class="staged-remove" onclick="removeStagedFile(${i})">&#10005;</button>
    </div>`).join("");
  hint.style.display = "";
}

function removeStagedFile(idx) { stagedFiles.splice(idx, 1); renderStaged(); }
function clearStaged()          { stagedFiles = []; renderStaged(); }

function tsStamp() { return new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19); }

function textToFile(text) {
  return new File([new Blob([text], { type: "text/plain" })], `clipboard_${tsStamp()}.txt`, { type: "text/plain" });
}

async function pasteFromClipboard() {
  try {
    if (navigator.clipboard.read) {
      try {
        const items = await navigator.clipboard.read();
        const files = [];
        let text = null;
        for (const item of items) {
          for (const type of item.types) {
            if (type.startsWith("image/")) {
              const blob = await item.getType(type);
              const ext = (type.split("/")[1] || "png").replace("jpeg", "jpg");
              files.push(new File([blob], `clipboard_${tsStamp()}.${ext}`, { type }));
            } else if (type === "text/plain" && text === null) {
              text = await (await item.getType(type)).text();
            }
          }
        }
        if (files.length)       { uploadFiles(files); return; }
        if (text && text.trim()) { uploadFiles([textToFile(text)]); return; }
      } catch { /* clipboard.read() unsupported/denied — fall back below */ }
    }
    const text = await navigator.clipboard.readText();
    if (!text.trim()) { alert("Clipboard is empty or contains no text or images."); return; }
    uploadFiles([textToFile(text)]);
  } catch (e) {
    alert("Could not read clipboard: " + e.message);
  }
}

// Global Cmd+V / Ctrl+V — works anywhere on the page, without stealing normal
// text paste from input fields (only intercepts when the clipboard has files).
document.addEventListener("paste", (e) => {
  const items = e.clipboardData?.items;
  if (!items) return;
  const files = [];
  for (const item of items) {
    if (item.kind === "file") {
      const f = item.getAsFile();
      if (f) files.push(f);
    }
  }
  if (!files.length) return; // no files on clipboard — let native paste (e.g. into a text field) proceed
  e.preventDefault();
  uploadFiles(files.map(f => {
    if (f.name && f.name !== "image.png" && f.name !== "blob") return f;
    const ext = (f.type.split("/")[1] || "bin").replace("jpeg", "jpg");
    return new File([f], `clipboard_${tsStamp()}.${ext}`, { type: f.type });
  }));
});

// ── browse (server-side file picker) ─────────────────────────────────────────
const BROWSE_LAST_PATH_KEY = "localsend_browse_path";
let browseState = { path: null, parent: null, entries: [] };
let browseSelected = new Map(); // path → {name, path, size}
let browseSuggestEntries = [];
let browseSuggestIndex = -1;
let browseSuggestTimer = null;
let browseSuggestBase = null;

async function openBrowseModal() {
  browseSelected = new Map();
  hideBrowseSuggestions();
  await browseTo(localStorage.getItem(BROWSE_LAST_PATH_KEY) || "");
  openModal("browse-modal");
}

async function browseTo(path) {
  try {
    const res = await GET(`/api/browse?path=${encodeURIComponent(path || "")}`);
    if (res.is_file) {
      // A full file path was typed/pasted: select it instead of trying to
      // navigate "into" it, then show the containing folder as usual.
      browseSelected.set(res.path, { name: res.name, path: res.path, size: res.size });
      await browseTo(res.parent);
      return;
    }
    browseState = res;
    localStorage.setItem(BROWSE_LAST_PATH_KEY, res.path);
    renderBrowse();
  } catch (e) {
    alert("Browse failed: " + e.message);
  }
}

function browseUp() {
  if (browseState.parent) browseTo(browseState.parent);
}

function onBrowsePathInput(value) {
  clearTimeout(browseSuggestTimer);
  browseSuggestTimer = setTimeout(() => fetchBrowseSuggestions(value), 150);
}

async function fetchBrowseSuggestions(value) {
  try {
    const res = await GET(`/api/browse/suggest?path=${encodeURIComponent(value || "")}`);
    browseSuggestEntries = res.entries;
    browseSuggestBase = res.base;
    browseSuggestIndex = -1;
    renderBrowseSuggestions();
  } catch (e) {
    hideBrowseSuggestions();
  }
}

function renderBrowseSuggestions() {
  const box = document.getElementById("browse-suggest");
  if (!browseSuggestEntries.length) {
    box.classList.remove("show");
    box.innerHTML = "";
    return;
  }
  box.classList.add("show");
  box.innerHTML = browseSuggestEntries.map((e, i) => `
    <div class="browse-suggest-item ${i === browseSuggestIndex ? "active" : ""}" onmousedown="event.preventDefault(); pickBrowseSuggestion(${i})">
      <span class="browse-icon">${e.is_dir ? "&#128193;" : "&#128196;"}</span>
      <span class="browse-name" title="${esc(e.name)}">${esc(e.name)}</span>
      ${e.is_dir ? "" : `<span class="browse-size">${fmtSize(e.size)}</span>`}
    </div>`).join("");
}

function pickBrowseSuggestion(i) {
  const e = browseSuggestEntries[i];
  if (!e) return;
  const base = browseSuggestBase;
  hideBrowseSuggestions();
  if (e.is_dir) {
    browseTo(e.path);
  } else {
    browseSelected.set(e.path, { name: e.name, path: e.path, size: e.size });
    browseTo(base); // jump to the file's folder so the selection is visible
  }
}

function hideBrowseSuggestions() {
  browseSuggestEntries = [];
  browseSuggestIndex = -1;
  const box = document.getElementById("browse-suggest");
  if (box) { box.classList.remove("show"); box.innerHTML = ""; }
}

function onBrowsePathKeydown(event) {
  if (event.key === "Enter") {
    event.preventDefault();
    if (browseSuggestIndex >= 0) pickBrowseSuggestion(browseSuggestIndex);
    else { hideBrowseSuggestions(); browseTo(event.target.value); }
  } else if (event.key === "ArrowDown" && browseSuggestEntries.length) {
    event.preventDefault();
    browseSuggestIndex = Math.min(browseSuggestIndex + 1, browseSuggestEntries.length - 1);
    renderBrowseSuggestions();
  } else if (event.key === "ArrowUp" && browseSuggestEntries.length) {
    event.preventDefault();
    browseSuggestIndex = Math.max(browseSuggestIndex - 1, 0);
    renderBrowseSuggestions();
  } else if (event.key === "Escape") {
    hideBrowseSuggestions();
  }
}

function onBrowseGoClick() {
  hideBrowseSuggestions();
  browseTo(document.getElementById("browse-path-input").value);
}

function renderBrowse() {
  document.getElementById("browse-path-input").value = browseState.path;
  document.getElementById("browse-up-btn").disabled = !browseState.parent;
  hideBrowseSuggestions();

  const list = document.getElementById("browse-list");
  if (!browseState.entries.length) {
    list.innerHTML = '<div class="empty">Empty folder</div>';
  } else {
    list.innerHTML = browseState.entries.map(e => {
      if (e.is_dir) {
        return `
        <div class="browse-item browse-dir" onclick="browseTo('${escAttr(e.path)}')">
          <span class="browse-icon">&#128193;</span>
          <span class="browse-name" title="${esc(e.name)}">${esc(e.name)}</span>
        </div>`;
      }
      const checked = browseSelected.has(e.path) ? "checked" : "";
      return `
      <div class="browse-item">
        <input type="checkbox" ${checked} onchange="toggleBrowseSelect('${escAttr(e.path)}', '${escAttr(e.name)}', ${e.size}, this.checked)">
        <span class="browse-icon">&#128196;</span>
        <span class="browse-name" title="${esc(e.name)}">${esc(e.name)}</span>
        <span class="browse-size">${fmtSize(e.size)}</span>
      </div>`;
    }).join("");
  }
  updateBrowseSelectionUI();
}

function toggleBrowseSelect(path, name, size, checked) {
  if (checked) browseSelected.set(path, { name, path, size });
  else browseSelected.delete(path);
  updateBrowseSelectionUI();
}

function updateBrowseSelectionUI() {
  const n = browseSelected.size;
  document.getElementById("browse-selected-count").textContent = n ? `${n} selected` : "";
  document.getElementById("browse-add-btn").disabled = !n;
}

function addBrowseSelection() {
  for (const f of browseSelected.values()) {
    stagedFiles.push({
      file_id: `browsed-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      name: f.name,
      path: f.path,
      size: f.size,
      origin: "browsed",
    });
  }
  renderStaged();
  closeModal("browse-modal");
}

// ── sync (ping + send + receive combined) ────────────────────────────────────
async function syncPartner(partnerId) {
  const initial = partners.find(x => x.id === partnerId);
  const route = (initial && initial.route) || "local";

  // Route is fixed to "cloud" — always take that path, skip probing direct
  // connectivity at all.
  if (route === "cloud") {
    return cloudSyncPartner(partnerId);
  }

  // A click is also an ad-hoc ping — fires even with nothing staged to send.
  // If the saved address doesn't answer, scan for it automatically (no extra click).
  if (config.role === "client") {
    const label = initial ? `${initial.name} (${initial.ip}:${initial.port})` : partnerId;
    try {
      console.log(`[sync] pinging ${label}…`);
      const chk = await GET(`/api/partners/${partnerId}/ping`);
      if (!chk.online) {
        console.log(`[sync] ${label} didn't answer — scanning to relocate it`);
        setDotScanning(partnerId, true);
        try {
          const res = await POST(`/api/partners/${partnerId}/discover`, {});
          console.log(`[sync] scan result for ${label}:`, res);
        } catch (e) {
          console.log(`[sync] scan failed for ${label}:`, e.message);
        } finally {
          setDotScanning(partnerId, false);
        }
      }
      refreshPartnerStatus();
    } catch (e) {
      console.log(`[sync] ping failed for ${label}:`, e.message);
    }
  }

  // Route "auto": once the connectivity check above shows this partner isn't
  // green, route through the cloud instead — switches back to direct on its
  // own next time, since this re-checks live status every call.
  const current = partners.find(x => x.id === partnerId);
  if (route === "auto" && current && current.status !== "green") {
    console.log(`[sync] auto route falling back to cloud for ${current.name} (status: ${current.status})`);
    return cloudSyncPartner(partnerId);
  }

  if (stagedFiles.length) {
    await sendToPartner(partnerId);
  }
  await receiveFromPartner(partnerId);
}

function setDotScanning(partnerId, on) {
  const dot = document.getElementById(`dot-${partnerId}`);
  if (!dot) return;
  dot.classList.toggle("scanning", on);
  if (on) dot.title = "scanning the network for it…";
}

async function sendToPartner(partnerId) {
  if (!stagedFiles.length) return;
  try {
    await POST("/api/send", { partner_id: partnerId, files: stagedFiles });
    stagedFiles = [];
    renderStaged();
  } catch (e) {
    alert(`Send failed: ${e.message}`);
  }
}

async function receiveFromPartner(partnerId) {
  try {
    await POST(`/api/receive/${partnerId}`);
  } catch (e) {
    alert(`Receive failed: ${e.message}`);
  }
}

// ── cloud fallback (manual — direct pipe is down) ────────────────────────────
async function cloudSendToPartner(partnerId) {
  if (!stagedFiles.length) return;
  try {
    await POST("/api/cloud/send", { partner_id: partnerId, files: stagedFiles });
    stagedFiles = [];
    renderStaged();
  } catch (e) {
    alert(`Cloud send failed: ${e.message}`);
  }
}

async function cloudReceiveFromPartner(partnerId) {
  try {
    await POST(`/api/cloud/receive/${partnerId}`);
  } catch (e) {
    alert(`Cloud receive failed: ${e.message}`);
  }
}

async function cloudSyncPartner(partnerId) {
  if (stagedFiles.length) await cloudSendToPartner(partnerId);
  await cloudReceiveFromPartner(partnerId);
}

async function abortTransfer(transferId) {
  try { await POST(`/api/abort/${transferId}`); } catch {}
}

// ── open file / folder ────────────────────────────────────────────────────────
async function openFile(path) {
  try { await POST("/api/open", { path, type: "file" }); }
  catch (e) { alert("Open failed: " + e.message); }
}

async function openFolder(path) {
  try { await POST("/api/open", { path, type: "folder" }); }
  catch (e) { alert("Open failed: " + e.message); }
}

// Log-entry open helpers (path stored in logPaths by entry id)
function openLogFile(id)   { const p = logPaths[id]; if (p) openFile(p); }
function openLogFolder(id) { const p = logPaths[id]; if (p) openFolder(p); }

// Toast open helpers (path stored in toastStore by toast id)
function openToastFile(id)   { const d = toastStore[id]; if (d?.saved_path) openFile(d.saved_path); }
function openToastFolder(id) { const d = toastStore[id]; if (d?.saved_path) openFolder(d.saved_path); }

// Toast clipboard copy / preview expand
function copyToastText(id) {
  const text = (toastStore[id] || {}).text_preview;
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById(`${id}-copybtn`);
    if (btn) { const orig = btn.textContent; btn.textContent = "Copied!"; setTimeout(() => { btn.textContent = orig; }, 1500); }
  });
}

function expandToastPreview(id) {
  const text = (toastStore[id] || {}).text_preview;
  if (!text) return;
  const el = document.getElementById(`${id}-preview`);
  if (el) el.textContent = text;
  const btn = document.getElementById(`${id}-expandbtn`);
  if (btn) btn.remove();
}

// ── toasts ────────────────────────────────────────────────────────────────────
function showToast(data) {
  const container = document.getElementById("toast-container");
  const id = `toast-${Date.now()}`;
  toastStore[id] = data;

  const div = document.createElement("div");
  div.className = "toast";
  div.id = id;
  let countdown = 30;

  const preview = data.text_preview;
  const previewShort = preview ? preview.slice(0, 200) : null;
  const needsExpand  = preview && preview.length > 200;

  div.innerHTML = `
    <div class="toast-title">&#8595; File received</div>
    <div class="toast-filename" title="${esc(data.saved_as || data.filename)}">${esc(data.saved_as || data.filename)}</div>
    <div class="toast-from">from ${esc(data.partner_name)}</div>
    ${preview ? `
    <div class="toast-preview">
      <div class="toast-preview-text" id="${id}-preview">${esc(previewShort)}${needsExpand ? "…" : ""}</div>
      <div class="toast-preview-actions">
        ${needsExpand ? `<button class="btn btn-sm btn-outline" id="${id}-expandbtn" onclick="expandToastPreview('${id}')">Show all</button>` : ""}
        <button class="btn btn-sm btn-outline" id="${id}-copybtn" onclick="copyToastText('${id}')">Copy text</button>
      </div>
    </div>` : ""}
    <div class="toast-actions" style="margin-top:8px">
      ${data.saved_path ? `
      <button class="btn btn-sm btn-outline" id="${id}-openbtn" onclick="openToastFile('${id}')">Open</button>
      <button class="btn btn-sm btn-outline" onclick="openToastFolder('${id}')">Show in Folder</button>` : ""}
      <span class="toast-timer" id="${id}-timer">${countdown}s</span>
      <button class="btn btn-sm btn-danger" onclick="dismissToast('${id}')">&#10005;</button>
    </div>`;

  container.appendChild(div);

  const timer = setInterval(() => {
    countdown--;
    const timerEl = document.getElementById(`${id}-timer`);
    if (timerEl) timerEl.textContent = countdown + "s";
    if (countdown <= 0) { clearInterval(timer); dismissToast(id); }
  }, 1000);
  div._timer = timer;
}

function dismissToast(id) {
  const el = document.getElementById(id);
  if (el) { clearInterval(el._timer); el.remove(); }
  delete toastStore[id];
}

// ── transfer bars ─────────────────────────────────────────────────────────────
function addTransferBar(key, dir, filename, partnerName, percent, transferId) {
  if (activeTransfers[key]) return;
  const list = document.getElementById("transfers-list");
  const el = document.createElement("div");
  el.className = "transfer-item";
  el.id = `tf-${safeId(key)}`;
  el.innerHTML = transferBarHTML(dir, filename, partnerName, percent, "", transferId);
  list.appendChild(el);
  activeTransfers[key] = el;
  document.getElementById("transfers-card").style.display = "";
}

function updateTransferBar(key, percent) {
  const el = activeTransfers[key]; if (!el) return;
  const bar = el.querySelector(".progress-bar");
  const pct = el.querySelector(".pct-text");
  if (bar) bar.style.width = percent + "%";
  if (pct) pct.textContent = percent + "%";
}

function updateTransferBarLabel(key, label) {
  const el = activeTransfers[key]; if (!el) return;
  const pct = el.querySelector(".pct-text");
  if (pct) pct.textContent = label;
}

function completeTransferBar(key, note) {
  const el = activeTransfers[key]; if (!el) return;
  const bar = el.querySelector(".progress-bar");
  const pct = el.querySelector(".pct-text");
  const abortBtn = el.querySelector(".btn-abort");
  if (bar) { bar.style.width = "100%"; }
  if (pct) pct.textContent = "✓ " + note;
  if (abortBtn) abortBtn.remove();
}

function errorTransferBar(key, errMsg) {
  const el = activeTransfers[key]; if (!el) return;
  const bar = el.querySelector(".progress-bar");
  const pct = el.querySelector(".pct-text");
  const abortBtn = el.querySelector(".btn-abort");
  if (bar) bar.classList.add("pb-error");
  if (pct) pct.textContent = "✗ " + errMsg;
  if (abortBtn) abortBtn.remove();
}

function removeTransferBar(key) {
  const el = activeTransfers[key]; if (!el) return;
  el.remove(); delete activeTransfers[key];
  if (!Object.keys(activeTransfers).length)
    document.getElementById("transfers-card").style.display = "none";
}

function transferBarHTML(dir, filename, partnerName, percent, note, transferId) {
  const dirLabel = dir === "send" ? "&#8593; Sending" : "&#8595; Receiving";
  const dirClass = dir === "send" ? "dir-send" : "dir-recv";
  const barClass = dir === "send" ? "pb-send"  : "pb-recv";
  return `
    <div class="transfer-header">
      <span class="transfer-filename" title="${esc(filename)}">${esc(filename)}</span>
      <span class="transfer-meta">${esc(partnerName)}</span>
    </div>
    <div class="transfer-header" style="margin-bottom:5px">
      <span class="transfer-dir ${dirClass}">${dirLabel}</span>
    </div>
    <div class="progress-bar-wrap">
      <div class="progress-bar ${barClass}" style="width:${percent}%"></div>
    </div>
    <div class="transfer-pct">
      <span class="pct-text">${percent}%</span>
      <button class="btn-abort" onclick="abortTransfer('${transferId}')" title="Abort">&#10005; abort</button>
    </div>`;
}

// ── log ───────────────────────────────────────────────────────────────────────
function renderLog(entries) {
  const el = document.getElementById("log-list");
  if (!entries.length) { el.innerHTML = '<div class="log-empty">No activity yet</div>'; document.getElementById("log-count").textContent = ""; return; }
  el.innerHTML = entries.slice(0, 100).map(logItemHTML).join("");
  document.getElementById("log-count").textContent = entries.length + " entries";
}

function prependLogEntry(entry) {
  const el = document.getElementById("log-list");
  const empty = el.querySelector(".log-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.innerHTML = logItemHTML(entry);
  el.insertBefore(div.firstElementChild, el.firstChild);
  while (el.children.length > 100) el.removeChild(el.lastChild);
  const cnt = document.getElementById("log-count");
  cnt.textContent = ((parseInt(cnt.textContent) || 0) + 1) + " entries";
}

function logItemHTML(e) {
  const isSent = e.direction === "sent";
  const isErr  = e.status === "error";
  const cls    = isErr ? "log-error" : isSent ? "log-sent" : "log-received";
  const arrow  = isErr ? "✗" : isSent ? "↑" : "↓";
  const verb   = isSent ? "to" : "from";

  if (e.path) logPaths[e.id] = e.path;

  const openBtns = (!isSent && !isErr && e.path) ? `
    <button class="log-open-btn" onclick="event.stopPropagation();openLogFile('${e.id}')" title="Open file">&#128194;</button>
    <button class="log-open-btn" onclick="event.stopPropagation();openLogFolder('${e.id}')" title="Show in Finder">&#128193;</button>` : "";

  return `
  <div class="log-item ${cls}" id="log-${e.id}">
    <span class="log-arrow">${arrow}</span>
    <span class="log-fname" title="${esc(e.filename)}">${esc(e.filename)}</span>
    <span class="log-partner">${esc(verb)} ${esc(e.partner_name)}</span>
    ${e.size ? `<span class="log-size">${fmtSize(e.size)}</span>` : ""}
    ${openBtns}
    <span class="log-time">${fmtTime(e.ts)}</span>
    <button class="log-del-btn" onclick="event.stopPropagation();deleteLogEntry('${e.id}')" title="Remove from log">&#10005;</button>
  </div>`;
}

async function deleteLogEntry(id) {
  try {
    await DEL(`/api/log/${id}`);
    const el = document.getElementById(`log-${id}`);
    if (el) el.remove();
    delete logPaths[id];
    const cnt = document.getElementById("log-count");
    const n = Math.max(0, (parseInt(cnt.textContent) || 0) - 1);
    cnt.textContent = n ? n + " entries" : "";
    const list = document.getElementById("log-list");
    if (!list.children.length) list.innerHTML = '<div class="log-empty">No activity yet</div>';
  } catch (e) { console.error("Delete log entry failed:", e); }
}

// ── modals ────────────────────────────────────────────────────────────────────
function openModal(id)  { document.getElementById(id).classList.add("show"); }
function closeModal(id) { document.getElementById(id).classList.remove("show"); }

document.querySelectorAll(".modal-backdrop").forEach(el => {
  el.addEventListener("click", e => { if (e.target === el) closeModal(el.id); });
});

["ap-ip", "ap-port"].forEach(id => {
  document.getElementById(id).addEventListener("keydown", e => { if (e.key === "Enter") submitAddPartner(); });
});

// ── utils ─────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
// Escapes a value for use inside a single-quoted JS string literal embedded in an HTML attribute
function escAttr(s) {
  return esc(String(s ?? "").replace(/\\/g, "\\\\").replace(/'/g, "\\'"));
}
function safeId(s) { return s.replace(/[^a-z0-9]/gi, "_"); }
function fmtSize(b) {
  if (!b) return "";
  if (b < 1024)           return b + " B";
  if (b < 1048576)        return (b/1024).toFixed(1) + " KB";
  if (b < 1073741824)     return (b/1048576).toFixed(1) + " MB";
  return (b/1073741824).toFixed(2) + " GB";
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso + (iso.endsWith("Z") ? "" : "Z")), now = new Date(), diff = now - d;
  if (diff < 60000)    return "just now";
  if (diff < 3600000)  return Math.floor(diff/60000) + "m ago";
  if (diff < 86400000) return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  return d.toLocaleDateString([], {month:"short", day:"numeric"});
}

// ── start ─────────────────────────────────────────────────────────────────────
init();
