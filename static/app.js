// ── state ─────────────────────────────────────────────────────────────────────
let config = {};
let partners = [];
let stagedFiles = [];   // {file_id, name, path, size}
let activeTransfers = {}; // key: `${transfer_id}:${filename}` → {el, dir, ...}
let partnerStatus = {};   // partner_id → true/false

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
    try {
      const data = await r.json();
      msg = data.detail || JSON.stringify(data);
    } catch {
      msg = await r.text().catch(() => r.statusText);
    }
    throw new Error(msg);
  }
  return r.json();
}
const GET = (p) => api("GET", p);
const POST = (p, b) => api("POST", p, b);
const PUT = (p, b) => api("PUT", p, b);
const DEL = (p) => api("DELETE", p);

// ── init ──────────────────────────────────────────────────────────────────────
async function init() {
  try {
    [config, partners] = await Promise.all([GET("/api/config"), GET("/api/partners")]);
    renderConfig();
    renderPartners();
    renderPartnerSelect();

    const [outbox, log] = await Promise.all([GET("/api/outbox"), GET("/api/log")]);
    // Restore in-progress send transfers from outbox
    outbox.forEach((t) => {
      t.files.forEach((f) => {
        const partner = partners.find((p) => p.id === t.partner_id);
        addTransferBar(`${t.transfer_id}:${f.name}`, "send", f.name, partner?.name || "?", 0);
      });
    });
    renderLog(log);
    connectSSE();
    pingAll();
    setupDropzone();
    setInterval(pingAll, 30000);
  } catch (e) {
    console.error("Init failed", e);
  }
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
}

function openSettings() {
  document.getElementById("cfg-name").value = config.device_name || "";
  document.getElementById("cfg-dir").value = config.receive_dir || "";
  openModal("settings-modal");
}

async function saveSettings() {
  try {
    config = await PUT("/api/config", {
      device_name: document.getElementById("cfg-name").value.trim(),
      receive_dir: document.getElementById("cfg-dir").value.trim(),
    });
    renderConfig();
    closeModal("settings-modal");
  } catch (e) {
    alert("Save failed: " + e.message);
  }
}

// ── partners ──────────────────────────────────────────────────────────────────
function renderPartners() {
  const el = document.getElementById("partners-list");
  if (!partners.length) {
    el.innerHTML = '<div class="empty">No partners yet — click + Add</div>';
    return;
  }
  el.innerHTML = partners
    .map((p) => {
      const online = partnerStatus[p.id];
      const dot = online === true ? "online" : online === false ? "offline" : "";
      return `
      <div class="partner-item" id="partner-${p.id}">
        <div class="status-dot ${dot}" title="${dot || "unknown"}"></div>
        <div class="partner-info">
          <div class="partner-name">${esc(p.name)}</div>
          <div class="partner-addr">${esc(p.ip)}:${p.port}</div>
        </div>
        <div class="partner-actions">
          <button class="btn btn-sm btn-outline" onclick="triggerPartner('${p.id}')" title="Send pending + check incoming">&#8646; Sync</button>
          <button class="btn btn-sm btn-danger" onclick="removePartner('${p.id}')">&#10005;</button>
        </div>
      </div>`;
    })
    .join("");
}

function renderPartnerSelect() {
  const sel = document.getElementById("partner-select");
  const prev = sel.value;
  sel.innerHTML = '<option value="">Select partner…</option>';
  partners.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    sel.appendChild(opt);
  });
  if (prev) sel.value = prev;
}

function openAddPartner() {
  document.getElementById("ap-name").value = "";
  document.getElementById("ap-ip").value = "";
  document.getElementById("ap-port").value = config.port || "8765";
  document.getElementById("ap-device-id").value = "";
  document.getElementById("ap-error").textContent = "";
  document.getElementById("ap-myid").textContent = config.device_id || "";
  openModal("add-partner-modal");
  setTimeout(() => document.getElementById("ap-ip").focus(), 50);
}

function copyMyId() {
  const id = config.device_id || "";
  navigator.clipboard.writeText(id).then(() => {
    const btn = event.target;
    btn.textContent = "✓ Copied";
    setTimeout(() => { btn.textContent = "Copy"; }, 1500);
  });
}

async function submitAddPartner() {
  const name = document.getElementById("ap-name").value.trim();
  const ip = document.getElementById("ap-ip").value.trim();
  const port = parseInt(document.getElementById("ap-port").value) || 8765;
  const device_id = document.getElementById("ap-device-id").value.trim() || null;
  const errEl = document.getElementById("ap-error");
  const btn = document.getElementById("ap-btn");

  if (!ip) { errEl.textContent = "IP address is required"; return; }

  btn.disabled = true;
  btn.textContent = "Saving…";
  errEl.textContent = "";

  try {
    const p = await POST("/api/partners", { name, ip, port, device_id });
    partners.push(p);
    renderPartners();
    renderPartnerSelect();
    closeModal("add-partner-modal");
    pingAll();
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Save Partner";
  }
}

async function removePartner(id) {
  if (!confirm("Remove this partner?")) return;
  await DEL(`/api/partners/${id}`);
  partners = partners.filter((p) => p.id !== id);
  renderPartners();
  renderPartnerSelect();
}

async function triggerPartner(id) {
  try {
    await POST(`/api/trigger/${id}`);
  } catch (e) {
    console.warn("Trigger failed:", e.message);
  }
}

async function pingAll() {
  if (!partners.length) return;
  try {
    const res = await GET("/api/partners/ping");
    partnerStatus = res;
    partners.forEach((p) => {
      const dot = document.querySelector(`#partner-${p.id} .status-dot`);
      if (!dot) return;
      dot.className = "status-dot " + (res[p.id] ? "online" : "offline");
      dot.title = res[p.id] ? "online" : "offline";
    });
  } catch {}
}

// ── SSE event listener ────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    const { type, data } = msg;

    if (type === "ping") return;

    if (type === "partners_update") {
      partners = data;
      renderPartners();
      renderPartnerSelect();
    } else if (type === "outbox_update") {
      // Outbox updated — no separate render needed, progress bars drive the UI
    } else if (type === "config_update") {
      config = data;
      renderConfig();
    } else if (type === "status") {
      const sb = document.getElementById("statusbar");
      if (data.receiving) {
        document.getElementById("status-text").textContent = `Receiving from ${data.partner}…`;
        sb.classList.add("show");
      } else {
        sb.classList.remove("show");
      }
    } else if (type === "send_progress") {
      const key = `${data.transfer_id}:${data.filename}`;
      updateTransferBar(key, data.percent);
    } else if (type === "receive_start") {
      data.files.forEach((f) => {
        const key = `${data.transfer_id}:${f.name}`;
        addTransferBar(key, "recv", f.name, data.partner_name, 0);
      });
    } else if (type === "receive_progress") {
      const key = `${data.transfer_id}:${data.filename}`;
      updateTransferBar(key, data.percent);
    } else if (type === "receive_complete") {
      const key = `${data.transfer_id}:${data.filename}`;
      completeTransferBar(key, `Saved as ${data.saved_as}`);
      setTimeout(() => removeTransferBar(key), 3000);
    } else if (type === "receive_error") {
      const key = `${data.transfer_id}:${data.filename}`;
      errorTransferBar(key, data.error);
      setTimeout(() => removeTransferBar(key), 5000);
    } else if (type === "log_entry") {
      prependLogEntry(data);
    }
  };

  es.onerror = () => {
    setTimeout(connectSSE, 3000);
    es.close();
  };
}

// ── drop zone / file staging ──────────────────────────────────────────────────
function setupDropzone() {
  const dz = document.getElementById("dropzone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("hover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("hover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("hover");
    uploadFiles(e.dataTransfer.files);
  });
}

function handleFileInput(input) {
  uploadFiles(input.files);
  input.value = "";
}

async function uploadFiles(fileList) {
  if (!fileList || !fileList.length) return;
  const fd = new FormData();
  for (const f of fileList) fd.append("files", f);

  try {
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    if (!res.ok) throw new Error(await res.text());
    const uploaded = await res.json();
    uploaded.forEach((f) => stagedFiles.push(f));
    renderStaged();
  } catch (e) {
    alert("Upload failed: " + e.message);
  }
}

function renderStaged() {
  const list = document.getElementById("staged-list");
  const controls = document.getElementById("send-controls");

  if (!stagedFiles.length) {
    list.innerHTML = "";
    controls.style.display = "none";
    return;
  }

  list.innerHTML = stagedFiles
    .map(
      (f, i) => `
    <div class="staged-file">
      <span class="fname" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="fsize">${fmtSize(f.size)}</span>
      <button class="staged-remove" onclick="removeStagedFile(${i})" title="Remove">&#10005;</button>
    </div>`
    )
    .join("");

  controls.style.display = "flex";
}

function removeStagedFile(idx) {
  stagedFiles.splice(idx, 1);
  renderStaged();
}

function clearStaged() {
  stagedFiles = [];
  renderStaged();
}

async function queueSend() {
  const partnerId = document.getElementById("partner-select").value;
  if (!partnerId) { alert("Select a partner first"); return; }
  if (!stagedFiles.length) { alert("No files staged"); return; }

  try {
    const t = await POST("/api/queue", { partner_id: partnerId, files: stagedFiles });
    // Add send progress bars
    const partner = partners.find((p) => p.id === partnerId);
    t.files.forEach((f) => {
      addTransferBar(`${t.transfer_id}:${f.name}`, "send", f.name, partner?.name || "?", 0);
    });
    stagedFiles = [];
    renderStaged();
  } catch (e) {
    alert("Queue failed: " + e.message);
  }
}

// ── transfer progress bars ────────────────────────────────────────────────────
function addTransferBar(key, dir, filename, partnerName, percent) {
  if (activeTransfers[key]) return;
  const list = document.getElementById("transfers-list");
  const el = document.createElement("div");
  el.className = "transfer-item";
  el.id = `tf-${key.replace(/[^a-z0-9]/gi, "_")}`;
  el.innerHTML = transferBarHTML(dir, filename, partnerName, percent, "");
  list.appendChild(el);
  activeTransfers[key] = el;
  document.getElementById("transfers-card").style.display = "";
}

function updateTransferBar(key, percent) {
  const el = activeTransfers[key];
  if (!el) return;
  const bar = el.querySelector(".progress-bar");
  const pct = el.querySelector(".transfer-pct");
  if (bar) bar.style.width = percent + "%";
  if (pct) pct.textContent = percent + "%";
}

function completeTransferBar(key, note) {
  const el = activeTransfers[key];
  if (!el) return;
  const bar = el.querySelector(".progress-bar");
  const pct = el.querySelector(".transfer-pct");
  if (bar) { bar.style.width = "100%"; bar.classList.add("pb-recv"); }
  if (pct) pct.textContent = "✓ " + note;
}

function errorTransferBar(key, errMsg) {
  const el = activeTransfers[key];
  if (!el) return;
  const bar = el.querySelector(".progress-bar");
  const pct = el.querySelector(".transfer-pct");
  if (bar) bar.classList.add("pb-error");
  if (pct) pct.textContent = "✗ " + errMsg;
}

function removeTransferBar(key) {
  const el = activeTransfers[key];
  if (!el) return;
  el.remove();
  delete activeTransfers[key];
  if (!Object.keys(activeTransfers).length) {
    document.getElementById("transfers-card").style.display = "none";
  }
}

function transferBarHTML(dir, filename, partnerName, percent, note) {
  const dirLabel = dir === "send" ? "↑ Sending" : "↓ Receiving";
  const dirClass = dir === "send" ? "dir-send" : "dir-recv";
  const barClass = dir === "send" ? "pb-send" : "pb-recv";
  return `
    <div class="transfer-header">
      <span class="transfer-filename" title="${esc(filename)}">${esc(filename)}</span>
      <span class="transfer-meta">${esc(partnerName)}</span>
    </div>
    <div class="transfer-header" style="margin-bottom:4px">
      <span class="transfer-dir ${dirClass}">${dirLabel}</span>
    </div>
    <div class="progress-bar-wrap">
      <div class="progress-bar ${barClass}" style="width:${percent}%"></div>
    </div>
    <div class="transfer-pct">${percent}%</div>`;
}

// ── log ───────────────────────────────────────────────────────────────────────
function renderLog(entries) {
  const el = document.getElementById("log-list");
  if (!entries.length) {
    el.innerHTML = '<div class="log-empty">No activity yet</div>';
    document.getElementById("log-count").textContent = "";
    return;
  }
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

  // Trim to 100
  while (el.children.length > 100) el.removeChild(el.lastChild);

  const cnt = document.getElementById("log-count");
  const n = parseInt(cnt.textContent) || 0;
  cnt.textContent = (n + 1) + " entries";
}

function logItemHTML(e) {
  const isSent = e.direction === "sent";
  const isErr = e.status === "error";
  const cls = isErr ? "log-error" : isSent ? "log-sent" : "log-received";
  const arrow = isErr ? "✗" : isSent ? "↑" : "↓";
  const verb = isSent ? "to" : "from";
  return `
  <div class="log-item ${cls}">
    <span class="log-arrow">${arrow}</span>
    <span class="log-fname" title="${esc(e.filename)}">${esc(e.filename)}</span>
    <span class="log-partner">${esc(verb)} ${esc(e.partner_name)}</span>
    ${e.size ? `<span class="log-size">${fmtSize(e.size)}</span>` : ""}
    <span class="log-time">${fmtTime(e.ts)}</span>
  </div>`;
}

// ── modals ────────────────────────────────────────────────────────────────────
function openModal(id) {
  document.getElementById(id).classList.add("show");
}
function closeModal(id) {
  document.getElementById(id).classList.remove("show");
}

// Close modal on backdrop click
document.querySelectorAll(".modal-backdrop").forEach((el) => {
  el.addEventListener("click", (e) => {
    if (e.target === el) closeModal(el.id);
  });
});

// Enter key in add partner form
["ap-name", "ap-ip", "ap-port", "ap-device-id"].forEach((id) => {
  document.getElementById(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitAddPartner();
  });
});

// ── utils ─────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtSize(bytes) {
  if (!bytes) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function fmtTime(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr + (isoStr.endsWith("Z") ? "" : "Z"));
  const now = new Date();
  const diff = now - d;
  if (diff < 60000) return "just now";
  if (diff < 3600000) return Math.floor(diff / 60000) + "m ago";
  if (diff < 86400000) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// ── start ─────────────────────────────────────────────────────────────────────
init();
