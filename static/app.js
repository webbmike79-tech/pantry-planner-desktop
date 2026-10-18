/* Pantry Planner PWA — vanilla JS frontend for the FastAPI backend. */
"use strict";

const KEY_STORAGE = "gemini_api_key";
const GROQ_KEY_STORAGE = "groq_api_key";
const OPENROUTER_KEY_STORAGE = "openrouter_api_key";
const HC_STORAGE = "daily_headcounts";
const SERVER_URL_STORAGE = "pantry_server_url";
const ACCESS_TOKEN_STORAGE = "pantry_access_token";
const CACHE_INV_STORAGE = "pantry_cache_inventory";
const CACHE_SETTINGS_STORAGE = "pantry_cache_settings";
const CACHE_PLAN_STORAGE = "pantry_cache_plan";
const CACHE_TIME_STORAGE = "pantry_cache_time";
const OUTBOX_STORAGE = "pantry_outbox";
const SYNCED_IDS_STORAGE = "pantry_synced_client_ids";

const LOCATIONS = ["Fridge", "Freezer", "Cupboard / Pantry"];
const CATEGORIES = [
  "Produce/Veggies", "Protein", "Dairy", "Seasoning/Spice",
  "Drink/Beverage", "Pantry/Dry", "Frozen",
];
const DIET_TAGS = [
  "Balanced & Whole Foods",
  "Diabetic / Low Glycemic",
  "Anti-Inflammatory / Low Saturated Fat",
  "Gluten-Free",
  "Dairy-Free / Lactose Intolerant",
  "Nut / Peanut Allergy",
  "Low Sodium / Heart Healthy",
  "Low Carb / Keto",
  "Vegetarian",
  "Vegan",
];
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const state = {
  inventory: [],
  settings: null,
  planId: null,
  planCreatedAt: null,
  plan: null,
  scanLocation: "Fridge",
};

let connectionState = "CONNECTING"; // "CONNECTED" | "CONNECTING" | "DISCONNECTED" | "AUTH_REQUIRED"
let lastSyncTime = localStorage.getItem(CACHE_TIME_STORAGE) || "";
let heartbeatTimer = null;
let isFlushingOutbox = false;

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  if (!t) return;
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

function setMsg(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.className = "form-msg" + (kind ? " " + kind : "");
}

/* ---------------- Storage helpers ---------------- */

function storedKey() {
  return localStorage.getItem(KEY_STORAGE) || "";
}
function storedGroqKey() {
  return localStorage.getItem(GROQ_KEY_STORAGE) || "";
}
function storedOpenRouterKey() {
  return localStorage.getItem(OPENROUTER_KEY_STORAGE) || "";
}
function hasKey() {
  return storedKey().length > 0 || storedGroqKey().length > 0 || storedOpenRouterKey().length > 0;
}

function storedServerUrl() {
  return (localStorage.getItem(SERVER_URL_STORAGE) || "").trim().replace(/\/+$/, "");
}

function storedAccessToken() {
  return (localStorage.getItem(ACCESS_TOKEN_STORAGE) || "").trim();
}

function isCapacitor() {
  return typeof window.Capacitor !== "undefined" || window.location.protocol === "capacitor:";
}

function buildUrl(path) {
  const base = storedServerUrl();
  const cleanPath = path.startsWith("/") ? path : "/" + path;
  return base ? `${base}${cleanPath}` : cleanPath;
}

/* ---------------- Connection & State Machine ---------------- */

function isOnline() {
  return connectionState === "CONNECTED";
}

function updateConnectionUI() {
  const banner = $("#connection-banner");
  const bannerText = $("#banner-text");
  const bannerIcon = $("#banner-icon");
  const dot = $("#server-status-dot");
  const statusText = $("#server-status-text");
  const serverUrl = storedServerUrl();
  const hostLabel = serverUrl ? serverUrl.replace(/^https?:\/\//, "") : (isCapacitor() ? "Home Server" : "local server");

  if (banner) banner.className = "connection-banner";
  if (dot) dot.className = "status-dot";

  if (connectionState === "CONNECTED") {
    if (banner) banner.hidden = true;
    if (dot) dot.classList.add("connected");
    if (statusText) statusText.textContent = `Connected (${hostLabel})`;
  } else if (connectionState === "CONNECTING") {
    if (banner) banner.hidden = true;
    if (dot) dot.classList.add("connecting");
    if (statusText) statusText.textContent = "Connecting…";
  } else if (connectionState === "AUTH_REQUIRED") {
    if (banner) {
      banner.hidden = false;
      banner.classList.add("auth-err");
    }
    if (bannerIcon) bannerIcon.textContent = "🔒";
    if (bannerText) bannerText.textContent = `Access code required for ${hostLabel}. Set it in Settings.`;
    if (dot) dot.classList.add("auth");
    if (statusText) statusText.textContent = "Access code required / incorrect";
  } else {
    // DISCONNECTED
    if (banner) {
      banner.hidden = false;
    }
    if (bannerIcon) bannerIcon.textContent = "⚠️";
    const timeNote = lastSyncTime ? ` Showing cached data from ${lastSyncTime}.` : " Reconnecting…";
    if (bannerText) bannerText.textContent = `Disconnected from ${hostLabel}.${timeNote}`;
    if (dot) dot.classList.add("disconnected");
    if (statusText) statusText.textContent = `Disconnected (${hostLabel})`;
  }
}

function setConnectionState(newState, detail) {
  const oldState = connectionState;
  connectionState = newState;
  updateConnectionUI();

  if (oldState !== "CONNECTED" && newState === "CONNECTED") {
    hideSetup();
    toast("Connected to Home Server ✅");
    flushOutbox();
  }
}

async function probeHealth(serverUrl, token) {
  const base = (serverUrl || "").trim().replace(/\/+$/, "");
  const target = (base ? base : "") + "/health";
  const headers = {};
  if (token) headers["X-Pantry-Token"] = token;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 4000);
  try {
    const res = await fetch(target, { method: "GET", headers, signal: controller.signal });
    clearTimeout(timeoutId);
    if (res.status === 401) {
      return { ok: false, authRequired: true, error: "Access code required or incorrect." };
    }
    if (!res.ok) {
      return { ok: false, error: `Server returned error (${res.status}).` };
    }
    const data = await res.json();
    if (data.status === "ok" && (data.app === "pantry-planner" || data.status === "ok")) {
      return { ok: true, authRequired: data.auth_required || false };
    }
    return { ok: false, error: "Connected, but server is not Pantry Planner." };
  } catch (err) {
    clearTimeout(timeoutId);
    if (err.name === "AbortError") {
      return { ok: false, error: "Connection timed out. Check IP and Wi-Fi." };
    }
    return { ok: false, error: `Could not reach server: ${err.message}` };
  }
}

async function checkHeartbeat() {
  const result = await probeHealth(storedServerUrl(), storedAccessToken());
  if (result.ok) {
    if (connectionState !== "CONNECTED") {
      setConnectionState("CONNECTED");
      refreshAll(true);
    }
  } else if (result.authRequired) {
    setConnectionState("AUTH_REQUIRED");
  } else {
    if (connectionState === "CONNECTED") {
      setConnectionState("DISCONNECTED");
    }
  }
}

function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => {
    if (document.visibilityState === "visible") {
      checkHeartbeat();
    }
  }, 15000);
}

/* ---------------- Offline Cache & Outbox Queue ---------------- */

function loadFromOfflineCache() {
  try {
    const cachedInv = localStorage.getItem(CACHE_INV_STORAGE);
    if (cachedInv) {
      const parsed = JSON.parse(cachedInv);
      state.inventory = Array.isArray(parsed) ? parsed : [];
    }
    const cachedSettings = localStorage.getItem(CACHE_SETTINGS_STORAGE);
    if (cachedSettings) state.settings = JSON.parse(cachedSettings);
    const cachedPlan = localStorage.getItem(CACHE_PLAN_STORAGE);
    if (cachedPlan) {
      const p = JSON.parse(cachedPlan);
      state.planId = p.plan_id;
      state.planCreatedAt = p.created_at;
      state.plan = p.plan;
    }
    lastSyncTime = localStorage.getItem(CACHE_TIME_STORAGE) || "";
  } catch (e) {
    console.error("Failed parsing offline cache:", e);
  }
}

function saveToOfflineCache(inv, settings, latest) {
  try {
    const nowStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    lastSyncTime = nowStr;
    localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(inv));
    localStorage.setItem(CACHE_SETTINGS_STORAGE, JSON.stringify(settings));
    localStorage.setItem(CACHE_PLAN_STORAGE, JSON.stringify(latest));
    localStorage.setItem(CACHE_TIME_STORAGE, nowStr);
  } catch (e) {
    console.warn("Failed saving to offline cache:", e);
  }
}

function getOutbox() {
  try { return JSON.parse(localStorage.getItem(OUTBOX_STORAGE) || "[]"); } catch (e) { return []; }
}

function saveOutbox(items) {
  localStorage.setItem(OUTBOX_STORAGE, JSON.stringify(items));
  renderOutboxStatus();
}

function getSyncedIds() {
  try { return new Set(JSON.parse(localStorage.getItem(SYNCED_IDS_STORAGE) || "[]")); } catch (e) { return new Set(); }
}

function markSyncedId(id) {
  const ids = getSyncedIds();
  ids.add(id);
  const arr = Array.from(ids).slice(-100);
  localStorage.setItem(SYNCED_IDS_STORAGE, JSON.stringify(arr));
}

function renderOutboxStatus() {
  const outbox = getOutbox();
  const card = $("#outbox-status-card");
  const count = $("#outbox-count");
  if (card && count) {
    count.textContent = outbox.length;
    card.hidden = outbox.length === 0;
  }
}

async function flushOutbox() {
  if (isFlushingOutbox || !isOnline()) return;
  const outbox = getOutbox();
  if (!outbox.length) return;

  isFlushingOutbox = true;
  const remaining = [];
  let count = 0;

  for (const item of outbox) {
    const synced = getSyncedIds();
    if (synced.has(item.client_id)) continue;
    try {
      await api("/api/inventory", {
        method: "POST",
        body: {
          location: item.location,
          items: [{
            item_name: item.item_name,
            category: item.category,
            quantity: item.quantity,
            unit: item.unit,
          }],
        },
      });
      markSyncedId(item.client_id);
      count++;
    } catch (e) {
      console.warn("Failed syncing item from outbox:", item, e);
      remaining.push(item);
    }
  }

  saveOutbox(remaining);
  isFlushingOutbox = false;
  if (count > 0) {
    toast(`Synced ${count} queued item${count === 1 ? "" : "s"} to Home Server! ✅`);
    try {
      const inv = await api("/api/inventory");
      state.inventory = inv;
      localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(inv));
      renderInventory();
    } catch (e) {}
  }
}

function queueOfflineAdd(item) {
  const clientId = "outbox_" + Date.now() + "_" + Math.floor(Math.random() * 100000);
  const outbox = getOutbox();
  outbox.push({
    client_id: clientId,
    item_name: item.item_name,
    quantity: item.quantity,
    unit: item.unit,
    location: item.location,
    category: item.category,
    created_at: new Date().toISOString(),
  });
  saveOutbox(outbox);

  const localItem = Object.assign({}, item, {
    id: -1 * (outbox.length),
    _isQueued: true,
    client_id: clientId,
  });
  state.inventory.unshift(localItem);
  localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(state.inventory));
  renderInventory();
  toast("Added to offline queue (will sync when home) 📦");
}

/* ---------------- Provider key handling ---------------- */

async function validateProviderKey(provider, candidate) {
  const headers = { "Content-Type": "application/json" };
  const token = storedAccessToken();
  if (token) headers["X-Pantry-Token"] = token;
  const url = buildUrl("/api/validate-provider");
  try {
    const res = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ provider, api_key: candidate }),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (res.status === 400) {
      return { ok: false, error: data.detail || "No key provided." };
    }
    if (data.ok) return { ok: true, message: data.message };
    return { ok: false, error: data.error || "That key didn't work. Please check it and try again." };
  } catch (e) {
    return { ok: false, error: "Couldn't reach the server to check the key. Is the app running?" };
  }
}

async function saveProviderKeyWithValidation(provider, storageKey, inputEl, msgEl, onSuccess) {
  const candidate = inputEl.value.trim();
  if (!candidate) {
    localStorage.removeItem(storageKey);
    setMsg(msgEl, `Cleared ${provider} key.`, "ok");
    toast(`${provider} key removed`);
    return;
  }
  setMsg(msgEl, `Checking your key with ${provider}…`);
  const result = await validateProviderKey(provider, candidate);
  if (result.ok) {
    localStorage.setItem(storageKey, candidate);
    setMsg(msgEl, `✅ ${provider} key connected!`, "ok");
    toast(`${provider} key saved ✅`);
    if (onSuccess) onSuccess();
  } else {
    setMsg(msgEl, result.error, "err");
  }
}

async function validateKey(candidate) {
  return validateProviderKey("gemini", candidate);
}

/* Validate first; only store the key in localStorage after Google accepts it. */
async function saveKeyWithValidation(inputEl, msgEl, onSuccess) {
  const candidate = inputEl.value.trim();
  if (!candidate) {
    setMsg(msgEl, "Paste your Gemini API key first.", "err");
    return;
  }
  setMsg(msgEl, "Checking your key with Google…");
  try {
    const result = await validateKey(candidate);
    if (result.ok) {
      localStorage.setItem(KEY_STORAGE, candidate);
      setMsg(msgEl, "✅ Key works! You're connected.", "ok");
      toast("Gemini key connected ✅");
      if (onSuccess) onSuccess();
    } else {
      setMsg(msgEl, result.error, "err");
    }
  } catch (e) {
    setMsg(msgEl, "Couldn't reach the server to check the key. Is the app running?", "err");
  }
}

function showSetup() { $("#setup-screen").hidden = false; }
function hideSetup() { $("#setup-screen").hidden = true; }

function showServerSetup() { $("#server-setup-screen").hidden = false; }
function hideServerSetup() { $("#server-setup-screen").hidden = true; }

function showAddItemModal() {
  const modal = $("#add-item-modal");
  if (!modal) return;
  $("#add-item-name").value = "";
  $("#add-item-qty").value = "1";
  $("#add-item-unit").value = "count";
  $("#add-item-loc").value = state.scanLocation || "Cupboard / Pantry";
  modal.hidden = false;
  setTimeout(() => $("#add-item-name").focus(), 100);
}
function hideAddItemModal() {
  const modal = $("#add-item-modal");
  if (modal) modal.hidden = true;
}

function requireKey() {
  // Key ownership rule: when connected to a server, all AI calls are served by the
  // server's configured keys. A connected client never prompts for, requires, or blocks on keys.
  if (isOnline()) return true;
  if (hasKey()) return true;
  showSetup();
  toast("Add your free Gemini key first 🔑 (or connect to your desktop server in Settings)");
  return false;
}

function switchToTab(tabId) {
  document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabId));
  document.querySelectorAll(".tab-page").forEach((p) => p.classList.toggle("active", p.id === tabId));
  window.scrollTo({ top: 0 });
}

/* ---------------- API helper ---------------- */

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const key = storedKey();
  if (key) headers["X-Gemini-Key"] = key;
  const groqKey = storedGroqKey();
  if (groqKey) headers["X-Groq-Key"] = groqKey;
  const openrouterKey = storedOpenRouterKey();
  if (openrouterKey) headers["X-OpenRouter-Key"] = openrouterKey;
  const token = storedAccessToken();
  if (token) headers["X-Pantry-Token"] = token;

  let body = opts.body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }

  const url = buildUrl(path);
  let res;
  try {
    res = await fetch(url, { method: opts.method || "GET", headers, body });
  } catch (err) {
    setConnectionState("DISCONNECTED", err.message);
    throw new Error(`Cannot reach server at ${storedServerUrl() || "local"}. You appear to be offline.`);
  }

  if (res.status === 401) {
    setConnectionState("AUTH_REQUIRED", "Access code required or invalid.");
    throw new Error("Access code required or invalid. Check Desktop Server settings.");
  }

  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const j = await res.json();
      msg = j.detail || msg;
    } catch (e) { /* keep default */ }
    throw new Error(msg);
  }

  if (connectionState !== "CONNECTED") {
    setConnectionState("CONNECTED");
  }

  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

/* ---------------- Status bar ---------------- */

function renderStatus() {
  $("#stat-stock").textContent = state.inventory.length;
  $("#stat-headcount").textContent = state.settings ? state.settings.base_headcount : "–";
  let cooked = 0, toBuy = 0;
  if (state.plan) {
    cooked = (state.plan.menu || []).filter((d) => d.cooked).length;
    toBuy = (state.plan.shopping_list || []).filter((s) => !s.purchased).length;
  }
  $("#stat-cooked").textContent = `${cooked}/7`;
  $("#stat-tobuy").textContent = toBuy;
}

/* ---------------- Inventory (Tab 1) ---------------- */

function inventoryRow(item) {
  const row = document.createElement("div");
  row.className = "inv-row" + (item._isQueued ? " queued-row" : "");
  if (item.id != null) row.dataset.id = item.id;
  if (item._isQueued) row.dataset.queued = "1";

  const locOpts = LOCATIONS.map(
    (l) => `<option ${l === item.location ? "selected" : ""}>${esc(l)}</option>`).join("");
  const catOpts = CATEGORIES.map(
    (c) => `<option ${c === item.category ? "selected" : ""}>${esc(c)}</option>`).join("");
  
  const queuedBadge = item._isQueued ? '<span class="badge-queued">Queued</span>' : "";

  row.innerHTML = `
    <input class="inv-name" type="text" placeholder="Item name" value="${esc(item.item_name || "")}">
    ${queuedBadge}
    <input class="inv-qty" type="number" min="0" step="0.5" value="${item.quantity ?? 0}">
    <input class="inv-unit" type="text" placeholder="unit" value="${esc(item.unit || "count")}">
    <select class="inv-loc">${locOpts}</select>
    <select class="inv-cat">${catOpts}</select>
    <button class="inv-del" title="Remove item" aria-label="Remove item">✕</button>`;
  
  row.querySelector(".inv-del").addEventListener("click", () => row.remove());
  return row;
}

let currentInvViewMode = "onhand"; // "onhand" | "edit"

function setInventoryViewMode(mode) {
  currentInvViewMode = mode;
  const onhandContainer = $("#on-hand-container");
  const editorCard = $("#inventory-editor-card");
  const onhandBtn = $("#mode-onhand-btn");
  const editBtn = $("#mode-edit-btn");

  if (onhandBtn) onhandBtn.classList.toggle("active", mode === "onhand");
  if (editBtn) editBtn.classList.toggle("active", mode === "edit");

  if (onhandContainer) onhandContainer.hidden = (mode !== "onhand");
  if (editorCard) editorCard.hidden = (mode !== "edit");
}

function jumpToInventoryEditor(targetItemName) {
  setInventoryViewMode("edit");
  if (!targetItemName) return;
  // Find matching row in editor list and highlight/focus it
  setTimeout(() => {
    const rows = document.querySelectorAll(".inv-row");
    for (const r of rows) {
      const nameInput = r.querySelector(".inv-name");
      if (nameInput && nameInput.value.toLowerCase().trim() === targetItemName.toLowerCase().trim()) {
        r.scrollIntoView({ behavior: "smooth", block: "center" });
        nameInput.focus();
        r.style.outline = "2px solid var(--accent)";
        setTimeout(() => { r.style.outline = ""; }, 1800);
        break;
      }
    }
  }, 50);
}

function renderOnHandView() {
  const container = $("#on-hand-groups");
  const emptyNote = $("#on-hand-empty");
  if (!container) return;
  container.innerHTML = "";

  if (!Array.isArray(state.inventory) || state.inventory.length === 0) {
    if (emptyNote) emptyNote.style.display = "block";
    return;
  }
  if (emptyNote) emptyNote.style.display = "none";

  // Group definitions strictly covering Fridge / Freezer / Pantry-Cupboard
  const groups = [
    { key: "Fridge", label: "❄️ Fridge", matches: (loc) => (loc || "").toLowerCase().includes("fridge") },
    { key: "Freezer", label: "🧊 Freezer", matches: (loc) => (loc || "").toLowerCase().includes("freezer") },
    { key: "Cupboard / Pantry", label: "🥫 Pantry & Cupboard", matches: (loc) => !((loc || "").toLowerCase().includes("fridge") || (loc || "").toLowerCase().includes("freezer")) },
  ];

  for (const grp of groups) {
    // Filter items belonging to this location
    const items = state.inventory.filter((item) => grp.matches(item.location));
    if (!items.length) continue;

    // Sort alphabetically by item name
    items.sort((a, b) => (a.item_name || "").localeCompare(b.item_name || "", undefined, { sensitivity: "base" }));

    const grpCard = document.createElement("div");
    grpCard.className = "onhand-group";
    grpCard.dataset.location = grp.key;

    const grpTitle = document.createElement("div");
    grpTitle.className = "onhand-group-title";
    grpTitle.innerHTML = `
      <span>${grp.label}</span>
      <span class="onhand-count-pill">${items.length} item${items.length === 1 ? "" : "s"}</span>
    `;
    grpCard.appendChild(grpTitle);

    const listDiv = document.createElement("div");
    listDiv.className = "onhand-list";

    for (const item of items) {
      const itemRow = document.createElement("div");
      itemRow.className = "onhand-item";
      itemRow.title = `Tap to edit ${item.item_name}`;

      const isOut = (item.quantity == null || Number(item.quantity) <= 0);
      const qtyDisplay = isOut
        ? `<span class="onhand-qty out">out</span>`
        : `<span class="onhand-qty">${item.quantity} ${esc(item.unit || "count")}</span>`;

      const catDisplay = item.category ? `<span class="onhand-item-cat">• ${esc(item.category)}</span>` : "";
      const queuedBadge = item._isQueued ? ' <span class="badge-queued">Queued</span>' : "";

      itemRow.innerHTML = `
        <div class="onhand-item-left">
          <span class="onhand-item-name">${esc(item.item_name)}</span>
          ${catDisplay}
          ${queuedBadge}
        </div>
        <div class="onhand-item-right">
          ${qtyDisplay}
          <span class="onhand-edit-hint">✏️</span>
        </div>
      `;

      itemRow.addEventListener("click", () => {
        jumpToInventoryEditor(item.item_name);
      });

      listDiv.appendChild(itemRow);
    }

    grpCard.appendChild(listDiv);
    container.appendChild(grpCard);
  }
}

function renderInventory() {
  const list = $("#inventory-list");
  if (list) {
    list.innerHTML = "";
    if (!Array.isArray(state.inventory)) state.inventory = [];
    state.inventory.forEach((item) => list.appendChild(inventoryRow(item)));
  }
  const invEmpty = $("#inventory-empty");
  if (invEmpty) invEmpty.style.display = state.inventory.length ? "none" : "block";

  renderOnHandView();
  renderStatus();
}

function collectInventoryRows() {
  return Array.from(document.querySelectorAll(".inv-row")).map((row) => ({
    id: row.dataset.id ? parseInt(row.dataset.id, 10) : null,
    item_name: row.querySelector(".inv-name").value.trim(),
    quantity: parseFloat(row.querySelector(".inv-qty").value) || 0,
    unit: row.querySelector(".inv-unit").value.trim() || "count",
    location: row.querySelector(".inv-loc").value,
    category: row.querySelector(".inv-cat").value,
    updated_at: null,
  })).filter((r) => r.item_name.length > 0);
}

async function saveInventory() {
  const msg = $("#inv-msg");
  if (!isOnline()) {
    setMsg(msg, "Bulk editing is disabled while offline to prevent overwriting changes. Use ➕ Add Item to queue new additions.", "err");
    toast("⚠️ Reconnect to Home Server to save bulk edits.");
    return;
  }
  try {
    const data = await api("/api/inventory", { method: "PUT", body: collectInventoryRows() });
    state.inventory = data.inventory;
    localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(data.inventory));
    renderInventory();
    setMsg(msg, "✅ Inventory saved.", "ok");
    toast("Inventory updated!");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

async function submitAddItem() {
  const name = $("#add-item-name").value.trim();
  if (!name) {
    alert("Please enter an item name.");
    return;
  }
  const qty = parseFloat($("#add-item-qty").value) || 1;
  const unit = $("#add-item-unit").value.trim() || "count";
  const loc = $("#add-item-loc").value || state.scanLocation || "Cupboard / Pantry";
  const cat = $("#add-item-cat").value || "Pantry/Dry";

  const item = { item_name: name, quantity: qty, unit: unit, location: loc, category: cat };

  hideAddItemModal();

  if (isOnline()) {
    try {
      const data = await api("/api/inventory", {
        method: "POST",
        body: { location: loc, items: [item] },
      });
      state.inventory = data.inventory;
      localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(data.inventory));
      renderInventory();
      toast(`Added ${name} to ${loc}! ✅`);
    } catch (e) {
      // If it failed unexpectedly, fall back to offline queue
      queueOfflineAdd(item);
    }
  } else {
    queueOfflineAdd(item);
  }
}

/* ---------------- Diet & settings (Tab 2) ---------------- */

function buildDietForm() {
  const tags = $("#diet-tags");
  tags.innerHTML = "";
  DIET_TAGS.forEach((tag) => {
    const label = document.createElement("label");
    label.className = "check-line";
    label.innerHTML = `<input type="checkbox" value="${esc(tag)}"><span>${esc(tag)}</span>`;
    tags.appendChild(label);
  });

  const grid = $("#day-headcounts");
  grid.innerHTML = "";
  DAYS.forEach((day) => {
    const wrap = document.createElement("div");
    wrap.innerHTML = `<label for="hc-${day}">${day} Headcount</label>
      <input id="hc-${day}" type="number" min="1" max="100" value="2">`;
    grid.appendChild(wrap);
  });
}

function fillDietForm() {
  if (!state.settings) return;
  const s = state.settings;
  $("#base-headcount").value = s.base_headcount;
  document.querySelectorAll("#diet-tags input[type=checkbox]").forEach((cb) => {
    cb.checked = s.dietary_tags.includes(cb.value);
  });
  $("#custom-notes").value = s.custom_diet_notes || "";
  $("#staples").value = s.essential_staples || "";

  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(HC_STORAGE) || "{}"); } catch (e) { saved = {}; }
  DAYS.forEach((day) => {
    const el = document.getElementById(`hc-${day}`);
    el.value = saved[day] != null ? saved[day] : s.base_headcount;
  });
}

function collectDailyHeadcounts() {
  const out = {};
  DAYS.forEach((day) => {
    out[day] = parseInt(document.getElementById(`hc-${day}`).value, 10) || 1;
  });
  localStorage.setItem(HC_STORAGE, JSON.stringify(out));
  return out;
}

async function saveDiet() {
  const msg = $("#diet-msg");
  if (!isOnline()) {
    setMsg(msg, "Cannot update household diet settings while offline.", "err");
    toast("⚠️ Reconnect to Home Server to save settings.");
    return;
  }
  const body = {
    base_headcount: parseInt($("#base-headcount").value, 10) || 1,
    dietary_tags: Array.from(document.querySelectorAll("#diet-tags input:checked")).map((cb) => cb.value),
    custom_diet_notes: $("#custom-notes").value,
    essential_staples: $("#staples").value,
  };
  try {
    state.settings = await api("/api/settings", { method: "PUT", body });
    localStorage.setItem(CACHE_SETTINGS_STORAGE, JSON.stringify(state.settings));
    collectDailyHeadcounts();
    renderStatus();
    setMsg(msg, "✅ Household dietary preferences saved!", "ok");
    toast("Settings saved!");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

/* ---------------- Server Settings (Tab 2) ---------------- */

async function testServerConnection() {
  const urlInput = $("#server-url-input");
  const tokenInput = $("#server-token-input");
  const msg = $("#server-msg");
  const url = (urlInput.value || "").trim();
  const token = (tokenInput.value || "").trim();

  setMsg(msg, "Testing connection to server…");
  const res = await probeHealth(url, token);
  if (res.ok) {
    setMsg(msg, "✅ Connection successful! Server is ready.", "ok");
  } else if (res.authRequired) {
    setMsg(msg, `🔒 ${res.error}`, "err");
  } else {
    setMsg(msg, `❌ ${res.error}`, "err");
  }
}

async function saveServerSettings() {
  const urlInput = $("#server-url-input");
  const tokenInput = $("#server-token-input");
  const msg = $("#server-msg");
  let url = (urlInput.value || "").trim().replace(/\/+$/, "");
  const token = (tokenInput.value || "").trim();

  if (url && !url.startsWith("http://") && !url.startsWith("https://")) {
    url = "http://" + url;
    urlInput.value = url;
  }

  setMsg(msg, "Connecting…");
  const res = await probeHealth(url, token);
  if (!res.ok) {
    setMsg(msg, `❌ Cannot connect: ${res.error}`, "err");
    return;
  }

  localStorage.setItem(SERVER_URL_STORAGE, url);
  localStorage.setItem(ACCESS_TOKEN_STORAGE, token);
  setMsg(msg, "✅ Connected and saved!", "ok");
  toast("Connected to Home Server ✅");

  setConnectionState("CONNECTED");
  refreshAll();
}

/* ---------------- Menu (Tab 3) ---------------- */

function mealBlock(emoji, label, slotKey, meal, dayIdx, isCooked) {
  if (!meal) return "";
  const steps = (meal.prep_steps || []).map((s) => `<li>${esc(s)}</li>`).join("");
  const changedNote = meal.changed_from
    ? `<div class="changed-note">🔄 Changed from: <i>${esc(meal.changed_from)}</i></div>`
    : "";
  const swapBtn = !isCooked
    ? `<button class="swap-meal-btn" data-day="${dayIdx}" data-slot="${slotKey}" title="Change this meal">🔄 Change this meal</button>`
    : "";

  return `<div class="meal">
    <div class="meal-header">
      <h4>${emoji} ${label}: ${esc(meal.dish_name || "")}</h4>
      ${swapBtn}
    </div>
    ${changedNote}
    <span class="cook-time">⏱️ Cook Time: ${meal.cook_time_minutes ?? ""} mins</span>
    <details><summary>Recipe steps</summary><ol>${steps}</ol></details>
  </div>`;
}

function renderMenu() {
  const wrap = $("#menu-days");
  wrap.innerHTML = "";
  const has = !!(state.plan && (state.plan.menu || []).length);
  $("#menu-empty").style.display = has ? "none" : "block";
  $("#plan-meta").textContent = has ? `Plan generated: ${state.planCreatedAt}` : "";
  const notes = $("#nutrition-notes");
  if (has && state.plan.nutrition_notes) {
    notes.hidden = false;
    notes.innerHTML = `<b>Dietary &amp; Nutrition Note:</b> ${esc(state.plan.nutrition_notes)}`;
  } else {
    notes.hidden = true;
  }
  if (!has) { renderStatus(); return; }

  state.plan.menu.forEach((day, idx) => {
    const card = document.createElement("div");
    card.className = "day-card" + (day.cooked ? " cooked" : "");
    const deduct = (day.ingredients_to_deduct || [])
      .map((d) => `${esc(d.item_name)} (${d.amount_used} ${esc(d.unit)})`).join(", ");
    card.innerHTML = `
      <div class="day-head"><h3>${esc(day.day)} — Feeding ${day.headcount} People</h3>
      <span class="badge ${day.cooked ? "done" : "planned"}">${day.cooked ? "✅ COOKED" : "⏳ PLANNED"}</span></div>
      ${mealBlock("🌅", "Breakfast", "breakfast", day.breakfast, idx, day.cooked)}
      ${mealBlock("☀️", "Lunch", "lunch", day.lunch, idx, day.cooked)}
      ${mealBlock("🌙", "Dinner", "dinner", day.dinner, idx, day.cooked)}
      ${deduct ? `<p class="deduct-note"><b>Inventory to Deduct:</b> ${deduct}</p>` : ""}
      <button class="btn ${day.cooked ? "" : "btn-primary"} cook-btn" ${day.cooked ? "disabled" : ""}>
        ${day.cooked ? `✅ ${esc(day.day)} Cooked &amp; Deducted` : `🍳 Cooked ${esc(day.day)}'s Meals — Subtract from Inventory`}
      </button>`;
    const btn = card.querySelector(".cook-btn");
    if (!day.cooked) btn.addEventListener("click", () => cookDay(idx));

    card.querySelectorAll(".swap-meal-btn").forEach((sBtn) => {
      sBtn.addEventListener("click", () => {
        const dIdx = parseInt(sBtn.dataset.day, 10);
        const sSlot = sBtn.dataset.slot;
        swapMeal(dIdx, sSlot, sBtn);
      });
    });

    wrap.appendChild(card);
  });
  renderStatus();
}

async function swapMeal(dayIndex, slot, btnEl) {
  if (!requireKey()) return;
  if (!isOnline()) {
    toast("⚠️ Swapping a meal requires connection to your Home Server.");
    return;
  }
  const originalText = btnEl ? btnEl.textContent : "";
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "🔄 Finding meal…";
  }
  toast(`Re-picking ${slot} from on-hand ingredients…`);
  try {
    const data = await api("/api/plan/swap-meal", {
      method: "POST",
      body: { day_index: dayIndex, meal_slot: slot },
    });
    if (data.success) {
      state.plan = data.plan;
      localStorage.setItem(CACHE_PLAN_STORAGE, JSON.stringify({
        plan_id: state.planId,
        created_at: state.planCreatedAt,
        plan: state.plan,
      }));
      renderMenu();
      toast(data.message || "Meal changed!");
    } else {
      toast(data.message || "Nothing else on hand fits this slot");
    }
  } catch (e) {
    toast(`⚠️ ${e.message}`);
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = originalText;
    }
  }
}

async function generatePlan() {
  if (!requireKey()) return;
  if (!isOnline()) {
    toast("⚠️ Menu generation requires connection to your Home Server.");
    setMsg($("#menu-msg"), "Menu generation requires a live connection to your Home Server and Gemini AI.", "err");
    return;
  }
  const msg = $("#menu-msg");
  const btn = $("#generate-btn");
  btn.disabled = true;
  setMsg(msg, "Building allergy-compliant menu, portion scaling, and recipe steps…");
  try {
    const data = await api("/api/plan/generate", {
      method: "POST",
      body: { daily_headcounts: collectDailyHeadcounts(), theme_name: null },
    });
    state.planId = data.plan_id;
    state.planCreatedAt = data.created_at;
    state.plan = data.plan;
    localStorage.setItem(CACHE_PLAN_STORAGE, JSON.stringify(data));
    renderMenu();
    renderShopping();
    setMsg(msg, "✅ Weekly Menu & Recipes ready!", "ok");
  } catch (e) {
    setMsg(msg, e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

async function cookDay(dayIndex) {
  if (!isOnline()) {
    toast("⚠️ Cooking updates inventory and requires connection to your Home Server.");
    return;
  }
  try {
    const data = await api("/api/plan/cook", { method: "POST", body: { day_index: dayIndex } });
    state.plan = data.plan;
    state.planCreatedAt = data.created_at;
    state.inventory = data.inventory;
    localStorage.setItem(CACHE_PLAN_STORAGE, JSON.stringify({
      plan_id: state.planId,
      created_at: state.planCreatedAt,
      plan: state.plan,
    }));
    localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(state.inventory));
    renderMenu();
    renderInventory();
    toast(data.deducted && data.deducted.length
      ? `Subtracted ${data.deducted.length} items from inventory!`
      : "Marked as cooked!");
  } catch (e) {
    toast(e.message);
  }
}

/* ---------------- Shopping (Tab 4) ---------------- */

function renderShopping() {
  const wrap = $("#shopping-list");
  wrap.innerHTML = "";
  const items = state.plan ? (state.plan.shopping_list || []) : [];
  $("#shopping-empty").style.display = items.length ? "none" : "block";
  $("#shopping-empty").textContent = state.plan
    ? "🎉 Your kitchen inventory covers the entire week's menu!"
    : "No menu planned yet — your shopping list will build from your plan.";

  const cats = [...new Set(items.map((i) => i.category || "General"))].sort();
  cats.forEach((cat) => {
    const h = document.createElement("h4");
    h.className = "shop-cat";
    h.textContent = cat;
    wrap.appendChild(h);
    items.forEach((item, idx) => {
      if ((item.category || "General") !== cat) return;
      const label = document.createElement("label");
      label.className = "check-line shop-item" + (item.purchased ? " bought" : "");
      label.innerHTML = `
        <input type="checkbox" data-idx="${idx}" ${item.purchased ? "checked disabled" : ""}>
        <span class="shop-name"><b>${esc(item.item_name)}</b> — Need: ${esc(item.amount_needed)}</span>
        <small>Dest: ${esc(item.default_location || "Cupboard / Pantry")} | ${esc(item.reason || "")}</small>`;
      wrap.appendChild(label);
    });
  });
  wrap.querySelectorAll("input[type=checkbox]:not([disabled])")
    .forEach((cb) => cb.addEventListener("change", updateRestockBtn));
  updateRestockBtn();
  renderStatus();
}

function checkedShoppingIndices() {
  return Array.from(document.querySelectorAll("#shopping-list input[type=checkbox]:checked:not([disabled])"))
    .map((cb) => parseInt(cb.dataset.idx, 10));
}

function updateRestockBtn() {
  const n = checkedShoppingIndices().length;
  const btn = $("#restock-btn");
  btn.disabled = n === 0;
  btn.textContent = `📥 Add Checked Items (${n}) to Kitchen Inventory`;
  updateClearPurchasedBtn();
}

function purchasedCount() {
  const items = state.plan ? (state.plan.shopping_list || []) : [];
  return items.filter((i) => i.purchased).length;
}

function updateClearPurchasedBtn() {
  const n = purchasedCount();
  const btn = $("#clear-purchased-btn");
  btn.disabled = n === 0;
  btn.textContent = `🧹 Clear purchased items (${n})`;
}

async function clearPurchased() {
  if (!isOnline()) {
    toast("⚠️ Clearing purchased items requires connection to your Home Server.");
    return;
  }
  const msg = $("#shop-msg");
  const n = purchasedCount();
  if (n === 0) return;
  if (!confirm(`Remove ${n} purchased item${n === 1 ? "" : "s"} from the list?`)) return;
  try {
    const data = await api("/api/shopping/clear-purchased", { method: "POST" });
    state.plan = data.plan;
    localStorage.setItem(CACHE_PLAN_STORAGE, JSON.stringify({
      plan_id: state.planId,
      created_at: state.planCreatedAt,
      plan: state.plan,
    }));
    renderShopping();
    setMsg(msg, `✅ Cleared ${data.cleared} purchased item${data.cleared === 1 ? "" : "s"} from the list.`, "ok");
    toast("Purchased items cleared");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

async function restock() {
  if (!isOnline()) {
    toast("⚠️ Restocking items requires connection to your Home Server.");
    return;
  }
  const msg = $("#shop-msg");
  try {
    const data = await api("/api/shopping/restock", {
      method: "POST",
      body: { indices: checkedShoppingIndices() },
    });
    state.plan = data.plan;
    state.inventory = data.inventory;
    localStorage.setItem(CACHE_PLAN_STORAGE, JSON.stringify({
      plan_id: state.planId,
      created_at: state.planCreatedAt,
      plan: state.plan,
    }));
    localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(state.inventory));
    renderShopping();
    renderInventory();
    setMsg(msg, `✅ Added ${data.restocked.length} grocery items to your inventory!`, "ok");
    toast("Restocked!");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

async function exportList() {
  const msg = $("#shop-msg");
  try {
    const include = $("#export-include").checked ? "true" : "false";
    const md = await api(`/api/shopping-list/export?include_purchased=${include}`);
    const blob = new Blob([md], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "shopping_list.md";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setMsg(msg, "✅ Shopping list downloaded.", "ok");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

/* ---------------- Photo scans ---------------- */

// Storage for active chosen files (File objects or Blobs)
const selectedPhotoFiles = {
  scan: [],
  receipt: [],
};

function getCapacitorCamera() {
  if (typeof capacitorCamera !== "undefined" && capacitorCamera.Camera) {
    return capacitorCamera.Camera;
  }
  if (typeof window.Capacitor !== "undefined" && window.Capacitor.Plugins && window.Capacitor.Plugins.Camera) {
    return window.Capacitor.Plugins.Camera;
  }
  return null;
}

async function dataUrlToFile(dataUrl, filename) {
  const res = await fetch(dataUrl);
  const blob = await res.blob();
  return new File([blob], filename, { type: blob.type || "image/jpeg" });
}

function openPhotoChooserModal(title, allowMultiple, onPhotoTaken, onGalleryPicked) {
  const modal = $("#photo-picker-modal");
  const titleEl = $("#photo-picker-title");
  const descEl = $("#photo-picker-desc");
  const cameraBtn = $("#picker-camera-btn");
  const galleryBtn = $("#picker-gallery-btn");
  const cancelBtn = $("#picker-cancel-btn");

  if (!modal) return;
  titleEl.textContent = title || "Add Photo";
  descEl.textContent = allowMultiple
    ? "Take a photo with your camera or pick photos / screenshots from your gallery:"
    : "Take a photo with your camera or choose an existing photo from your gallery:";

  function close() {
    modal.hidden = true;
    cameraBtn.onclick = null;
    galleryBtn.onclick = null;
    cancelBtn.onclick = null;
  }

  cameraBtn.onclick = () => {
    close();
    onPhotoTaken();
  };
  galleryBtn.onclick = () => {
    close();
    onGalleryPicked();
  };
  cancelBtn.onclick = () => {
    close();
  };

  modal.hidden = false;
}

function applyChosenPhotos(flowKey, files, nameEl, previewEl, btn) {
  selectedPhotoFiles[flowKey] = files;
  if (!files || !files.length) {
    nameEl.textContent = "No photo chosen yet";
    previewEl.hidden = true;
    btn.disabled = true;
    return;
  }
  nameEl.textContent = files.length > 1
    ? `${files.length} photos chosen (${files[0].name}, …)`
    : files[0].name;
  previewEl.src = URL.createObjectURL(files[0]);
  previewEl.hidden = false;
  btn.disabled = false;
}

function wirePhotoInput(fileInput, nameEl, previewEl, btn, flowKey = "scan") {
  const labelEl = fileInput.closest("label.file-btn");
  const allowMultiple = fileInput.hasAttribute("multiple");

  // In standard browser / PWA: standard input change handler
  fileInput.addEventListener("change", () => {
    const files = Array.from(fileInput.files || []);
    if (files.length) {
      applyChosenPhotos(flowKey, files, nameEl, previewEl, btn);
    }
  });

  // In Capacitor (native app): intercept click to offer explicit Camera vs Gallery choice
  if (labelEl) {
    labelEl.addEventListener("click", async (e) => {
      const cameraPlugin = getCapacitorCamera();
      if (!isCapacitor() || !cameraPlugin) {
        // Run standard web file chooser
        return;
      }

      // Native Capacitor flow: prevent default file browser and show explicit modal
      e.preventDefault();
      e.stopPropagation();

      const modalTitle = flowKey === "scan" ? "📸 Shelf Photo" : "🧾 Receipt / Order Photo";

      openPhotoChooserModal(
        modalTitle,
        allowMultiple,
        // Option 1: Take photo with Camera
        async () => {
          try {
            const photo = await cameraPlugin.getPhoto({
              quality: 90,
              allowEditing: false,
              resultType: "dataUrl",
              source: "CAMERA",
            });
            if (photo && photo.dataUrl) {
              const file = await dataUrlToFile(photo.dataUrl, `camera_${Date.now()}.${photo.format || "jpeg"}`);
              applyChosenPhotos(flowKey, [file], nameEl, previewEl, btn);
            }
          } catch (err) {
            if (!String(err).includes("cancelled") && !String(err).includes("User cancelled")) {
              console.warn("Camera capture error:", err);
              toast("Camera unavailable: " + (err.message || err));
            }
          }
        },
        // Option 2: Choose from Gallery / Photos
        async () => {
          try {
            if (allowMultiple && cameraPlugin.pickImages) {
              const result = await cameraPlugin.pickImages({ quality: 90 });
              if (result && result.photos && result.photos.length) {
                const files = [];
                for (let i = 0; i < result.photos.length; i++) {
                  const p = result.photos[i];
                  let f;
                  if (p.webPath) {
                    const r = await fetch(p.webPath);
                    const b = await r.blob();
                    f = new File([b], `gallery_${i}_${Date.now()}.${p.format || "jpeg"}`, { type: b.type });
                  } else if (p.dataUrl) {
                    f = await dataUrlToFile(p.dataUrl, `gallery_${i}_${Date.now()}.${p.format || "jpeg"}`);
                  }
                  if (f) files.push(f);
                }
                if (files.length) {
                  applyChosenPhotos(flowKey, files, nameEl, previewEl, btn);
                }
                return;
              }
            }

            // Single photo gallery selection
            const photo = await cameraPlugin.getPhoto({
              quality: 90,
              allowEditing: false,
              resultType: "dataUrl",
              source: "PHOTOS",
            });
            if (photo && photo.dataUrl) {
              const file = await dataUrlToFile(photo.dataUrl, `gallery_${Date.now()}.${photo.format || "jpeg"}`);
              applyChosenPhotos(flowKey, [file], nameEl, previewEl, btn);
            }
          } catch (err) {
            if (!String(err).includes("cancelled") && !String(err).includes("User cancelled")) {
              console.warn("Gallery pick error:", err);
              toast("Gallery pick error: " + (err.message || err));
            }
          }
        }
      );
    });
  }
}

async function scanShelfPhoto() {
  if (!requireKey()) return;
  if (!isOnline()) {
    toast("⚠️ Photo scanning requires connection to your Home Server.");
    return;
  }
  const fileInput = $("#scan-file");
  const msg = $("#scan-msg");
  const btn = $("#scan-btn");

  const chosenFile = (selectedPhotoFiles.scan && selectedPhotoFiles.scan[0]) || (fileInput.files && fileInput.files[0]);
  if (!chosenFile) return;

  btn.disabled = true;
  setMsg(msg, `Scanning ${state.scanLocation} and estimating amounts…`);
  try {
    const fd = new FormData();
    fd.append("file", chosenFile);
    fd.append("location", state.scanLocation);
    const data = await api("/api/scan-photo", { method: "POST", body: fd });
    state.inventory = data.inventory;
    localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(data.inventory));
    renderInventory();
    const servedTag = data.served_by ? ` (served by ${esc(data.served_by)})` : "";
    setMsg(msg, `✅ Added ${data.added} items to ${state.scanLocation}!${servedTag}`, "ok");
    toast(`Added ${data.added} items!`);
  } catch (e) {
    setMsg(msg, e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

/* Shared success handling for all three receipt/order import paths. */
function handleReceiptResult(data, msg, doneLabel) {
  state.inventory = data.inventory;
  state.plan = data.plan;
  localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify(data.inventory));
  if (data.plan) {
    localStorage.setItem(CACHE_PLAN_STORAGE, JSON.stringify({
      plan_id: state.planId,
      created_at: state.planCreatedAt,
      plan: data.plan,
    }));
  }
  renderInventory();
  renderShopping();
  const servedTag = data.served_by ? ` (served by ${esc(data.served_by)})` : "";
  setMsg(msg, `✅ Restocked ${data.logs.length} items from ${data.store_name}${servedTag}:`, "ok");
  $("#receipt-logs").innerHTML = data.logs.map((entry) => {
    let html = esc(entry)
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/\*(.+?)\*/g, "<i>$1</i>");
    return `<p>${html}</p>`;
  }).join("");
  toast(doneLabel || "Receipt scanned!");
}

async function scanReceipt() {
  if (!requireKey()) return;
  if (!isOnline()) {
    toast("⚠️ Receipt scanning requires connection to your Home Server.");
    return;
  }
  const fileInput = $("#receipt-file");
  const msg = $("#receipt-msg");
  const btn = $("#receipt-btn");

  const chosenFiles = (selectedPhotoFiles.receipt && selectedPhotoFiles.receipt.length)
    ? selectedPhotoFiles.receipt
    : Array.from(fileInput.files || []);

  if (!chosenFiles.length) return;
  btn.disabled = true;
  setMsg(msg, "Reading receipt, decoding abbreviations, and updating inventory…");
  try {
    const fd = new FormData();
    for (const f of chosenFiles) fd.append("files", f);
    const data = await api("/api/receipt/scan", { method: "POST", body: fd });
    handleReceiptResult(data, msg, "Receipt scanned!");
  } catch (e) {
    setMsg(msg, e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

async function scanReceiptPdf() {
  if (!requireKey()) return;
  if (!isOnline()) {
    toast("⚠️ PDF importing requires connection to your Home Server.");
    return;
  }
  const fileInput = $("#receipt-pdf-file");
  const msg = $("#receipt-msg");
  const btn = $("#receipt-pdf-btn");
  if (!fileInput.files[0]) return;
  btn.disabled = true;
  setMsg(msg, "Reading your order PDF and updating inventory…");
  try {
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    const data = await api("/api/receipt/scan-pdf", { method: "POST", body: fd });
    handleReceiptResult(data, msg, "Order PDF imported!");
  } catch (e) {
    setMsg(msg, e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

async function pasteReceiptText() {
  if (!requireKey()) return;
  if (!isOnline()) {
    toast("⚠️ Pasting order text requires connection to your Home Server.");
    return;
  }
  const msg = $("#receipt-msg");
  const btn = $("#receipt-paste-btn");
  const text = $("#receipt-paste-text").value.trim();
  if (!text) {
    setMsg(msg, "Paste your order or receipt text first.", "err");
    return;
  }
  btn.disabled = true;
  setMsg(msg, "Reading your pasted order and updating inventory…");
  try {
    const data = await api("/api/receipt/paste", { method: "POST", body: { text } });
    handleReceiptResult(data, msg, "Order imported!");
  } catch (e) {
    setMsg(msg, e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- Tabs ---------------- */

function wireTabs() {
  document.querySelectorAll(".tabbar button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-page").forEach((p) => p.classList.toggle("active", p.id === btn.dataset.tab));
      window.scrollTo({ top: 0 });
    });
  });
}

/* ---------------- Init & Refresh ---------------- */

async function refreshAll(silent = false) {
  try {
    if (!silent) setConnectionState("CONNECTING");
    const [inv, settings, latest] = await Promise.all([
      api("/api/inventory"),
      api("/api/settings"),
      api("/api/plan/latest"),
    ]);
    state.inventory = Array.isArray(inv) ? inv : [];
    state.settings = settings;
    state.planId = latest.plan_id;
    state.planCreatedAt = latest.created_at;
    state.plan = latest.plan;

    saveToOfflineCache(inv, settings, latest);
    setConnectionState("CONNECTED");
    renderAll();
    await flushOutbox();
  } catch (err) {
    // Failed to connect live: fallback to offline cache
    loadFromOfflineCache();
    renderAll();
    if (!silent) {
      toast("Working offline (cached data loaded) 📦");
    }
  }
}

function renderAll() {
  renderInventory();
  fillDietForm();
  renderMenu();
  renderShopping();
  renderOutboxStatus();
}

function init() {
  // Service Worker policy:
  // Inside Capacitor, never run a Service Worker (Capacitor serves assets locally; SW caches cause stale asset bugs).
  // Purge any existing SW registrations and CacheStorage left from previous versions.
  if (isCapacitor()) {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.getRegistrations().then((regs) => {
        for (const reg of regs) {
          reg.unregister();
        }
      }).catch(() => {});
    }
    if ("caches" in window) {
      caches.keys().then((keys) => {
        for (const k of keys) {
          caches.delete(k);
        }
      }).catch(() => {});
    }
  }

  buildDietForm();
  wireTabs();

  // Populate Server Settings inputs
  const serverInput = $("#server-url-input");
  const tokenInput = $("#server-token-input");
  if (serverInput) serverInput.value = storedServerUrl();
  if (tokenInput) tokenInput.value = storedAccessToken();

  // Check if onboarding overlays should appear:
  // 1. If running inside Capacitor without a configured server URL, show server setup screen.
  // 2. In connected mode (server URL configured), NEVER prompt for, require, or block on keys.
  // 3. Only in standalone mode with no server URL and no stored keys, show first-run key setup.
  if (isCapacitor() && !storedServerUrl() && !sessionStorage.getItem("server_setup_skipped")) {
    showServerSetup();
  } else if (!storedServerUrl() && !hasKey() && !sessionStorage.getItem("setup_skipped")) {
    showSetup();
  }

  // AI Provider Keys setup events
  const groqInput = $("#groq-key-input");
  if (groqInput) groqInput.value = storedGroqKey();
  const openrouterInput = $("#openrouter-key-input");
  if (openrouterInput) openrouterInput.value = storedOpenRouterKey();

  const groqBtn = $("#groq-key-btn");
  if (groqBtn) {
    groqBtn.addEventListener("click", () => {
      saveProviderKeyWithValidation("groq", GROQ_KEY_STORAGE, $("#groq-key-input"), $("#groq-key-msg"));
    });
  }

  const openrouterBtn = $("#openrouter-key-btn");
  if (openrouterBtn) {
    openrouterBtn.addEventListener("click", () => {
      saveProviderKeyWithValidation("openrouter", OPENROUTER_KEY_STORAGE, $("#openrouter-key-input"), $("#openrouter-key-msg"));
    });
  }

  $("#settings-key-input").value = storedKey();
  $("#setup-save-btn").addEventListener("click", () => {
    saveKeyWithValidation($("#setup-key-input"), $("#setup-msg"), () => {
      $("#settings-key-input").value = storedKey();
      hideSetup();
    });
  });
  $("#setup-skip-btn").addEventListener("click", () => {
    sessionStorage.setItem("setup_skipped", "1");
    hideSetup();
  });
  $("#settings-key-btn").addEventListener("click", () => {
    saveKeyWithValidation($("#settings-key-input"), $("#settings-key-msg"));
  });

  // Server Setup Screen overlay events
  $("#setup-server-test-btn").addEventListener("click", async () => {
    const input = $("#setup-server-url");
    const tokenInput = $("#setup-server-token");
    const msg = $("#setup-server-msg");
    let val = (input.value || "").trim().replace(/\/+$/, "");
    const tok = (tokenInput.value || "").trim();
    if (!val) {
      setMsg(msg, "Enter your desktop server IP or URL first.", "err");
      return;
    }
    if (!val.startsWith("http://") && !val.startsWith("https://")) {
      val = "http://" + val;
      input.value = val;
    }
    setMsg(msg, "Checking connection…");
    const res = await probeHealth(val, tok);
    if (res.ok) {
      localStorage.setItem(SERVER_URL_STORAGE, val);
      localStorage.setItem(ACCESS_TOKEN_STORAGE, tok);
      setMsg(msg, "✅ Connected! Loading data…", "ok");
      setTimeout(() => {
        hideServerSetup();
        if (serverInput) serverInput.value = val;
        if (tokenInput) tokenInput.value = tok;
        refreshAll();
      }, 600);
    } else {
      setMsg(msg, `❌ Cannot connect: ${res.error}`, "err");
    }
  });

  $("#setup-server-skip-btn").addEventListener("click", () => {
    sessionStorage.setItem("server_setup_skipped", "1");
    hideServerSetup();
    refreshAll();
  });

  // Connection Banner action buttons
  $("#banner-retry-btn").addEventListener("click", () => {
    setConnectionState("CONNECTING");
    refreshAll();
  });
  $("#banner-settings-btn").addEventListener("click", () => {
    switchToTab("tab-diet");
    const card = $("#server-settings-card");
    if (card) card.scrollIntoView({ behavior: "smooth" });
  });

  // Server settings card buttons (Tab 2)
  $("#server-save-btn").addEventListener("click", saveServerSettings);
  $("#server-test-btn").addEventListener("click", testServerConnection);
  $("#outbox-sync-btn").addEventListener("click", () => {
    if (!isOnline()) {
      toast("⚠️ Still disconnected from Home Server.");
      return;
    }
    flushOutbox();
  });

  // Scan tab & Add Item modal
  document.querySelectorAll("#scan-location button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#scan-location button").forEach((b) => b.classList.toggle("active", b === btn));
      state.scanLocation = btn.dataset.loc;
    });
  });
  wirePhotoInput($("#scan-file"), $("#scan-file-name"), $("#scan-preview"), $("#scan-btn"), "scan");
  $("#scan-btn").addEventListener("click", scanShelfPhoto);

  const modeOnHandBtn = $("#mode-onhand-btn");
  if (modeOnHandBtn) {
    modeOnHandBtn.addEventListener("click", () => setInventoryViewMode("onhand"));
  }
  const modeEditBtn = $("#mode-edit-btn");
  if (modeEditBtn) {
    modeEditBtn.addEventListener("click", () => setInventoryViewMode("edit"));
  }

  $("#inv-add-btn").addEventListener("click", showAddItemModal);
  $("#add-item-confirm-btn").addEventListener("click", submitAddItem);
  $("#add-item-cancel-btn").addEventListener("click", hideAddItemModal);

  $("#inv-save-btn").addEventListener("click", saveInventory);
  $("#inv-clear-btn").addEventListener("click", async () => {
    if (!isOnline()) {
      toast("⚠️ Cannot clear inventory while disconnected.");
      return;
    }
    if (!confirm("Clear ALL inventory? This cannot be undone.")) return;
    try {
      await api("/api/inventory", { method: "DELETE" });
      state.inventory = [];
      localStorage.setItem(CACHE_INV_STORAGE, JSON.stringify([]));
      renderInventory();
      toast("Inventory cleared.");
    } catch (e) { toast(e.message); }
  });

  /* Diet tab */
  $("#diet-save-btn").addEventListener("click", saveDiet);

  /* Menu tab */
  $("#generate-btn").addEventListener("click", generatePlan);

  /* Shopping tab */
  $("#restock-btn").addEventListener("click", restock);
  $("#clear-purchased-btn").addEventListener("click", clearPurchased);
  $("#export-btn").addEventListener("click", exportList);
  wirePhotoInput($("#receipt-file"), $("#receipt-file-name"), $("#receipt-preview"), $("#receipt-btn"), "receipt");
  $("#receipt-btn").addEventListener("click", scanReceipt);
  const pdfInput = $("#receipt-pdf-file");
  pdfInput.addEventListener("change", () => {
    const f = pdfInput.files[0];
    if (f) {
      $("#receipt-pdf-name").textContent = f.name;
      $("#receipt-pdf-btn").disabled = false;
    }
  });
  $("#receipt-pdf-btn").addEventListener("click", scanReceiptPdf);
  $("#receipt-paste-btn").addEventListener("click", pasteReceiptText);

  /* Header refresh button */
  const headerRefreshBtn = $("#header-refresh-btn");
  if (headerRefreshBtn) {
    headerRefreshBtn.addEventListener("click", async () => {
      headerRefreshBtn.classList.add("spinning");
      try {
        await refreshAll(false);
        toast("Refreshed! ✅");
      } finally {
        headerRefreshBtn.classList.remove("spinning");
      }
    });
  }

  setupPullToRefresh();

  // Network and lifecycle events
  window.addEventListener("online", () => {
    checkHeartbeat();
  });
  window.addEventListener("offline", () => {
    setConnectionState("DISCONNECTED");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      checkHeartbeat();
    }
  });

  startHeartbeat();
  refreshAll();

  if (!isCapacitor() && "serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => { /* offline support optional */ });
  }
}

/* ---------------- Pull-to-refresh ---------------- */
function setupPullToRefresh() {
  const indicator = $("#ptr-indicator");
  const icon = indicator ? indicator.querySelector(".ptr-icon") : null;
  const text = indicator ? indicator.querySelector(".ptr-text") : null;
  if (!indicator) return;

  let startY = 0;
  let currentY = 0;
  let isTracking = false;
  let isRefreshing = false;
  const threshold = 65; // px of pull to trigger refresh

  function isEligible() {
    // Only allow pull-to-refresh when at the top of the page
    return window.scrollY <= 2 && document.documentElement.scrollTop <= 2;
  }

  window.addEventListener("touchstart", (e) => {
    if (isRefreshing) return;
    if (isEligible()) {
      startY = e.touches[0].clientY;
      currentY = startY;
      isTracking = true;
    }
  }, { passive: true });

  window.addEventListener("touchmove", (e) => {
    if (!isTracking || isRefreshing) return;
    currentY = e.touches[0].clientY;
    const diff = currentY - startY;

    if (diff > 0 && isEligible()) {
      indicator.classList.add("pulling");
      // Damped pull distance
      const pullDist = Math.min(diff * 0.45, 80);
      indicator.style.height = `${pullDist}px`;

      if (pullDist >= threshold * 0.45) {
        indicator.classList.add("ready");
        if (text) text.textContent = "Release to refresh…";
      } else {
        indicator.classList.remove("ready");
        if (text) text.textContent = "Pull to refresh…";
      }
    } else {
      indicator.classList.remove("pulling", "ready");
      indicator.style.height = "0px";
    }
  }, { passive: true });

  window.addEventListener("touchend", async () => {
    if (!isTracking || isRefreshing) return;
    isTracking = false;
    const diff = currentY - startY;
    const pullDist = diff * 0.45;

    if (pullDist >= threshold * 0.45 && isEligible()) {
      isRefreshing = true;
      indicator.classList.remove("pulling", "ready");
      indicator.classList.add("refreshing");
      indicator.style.height = "44px";
      if (text) text.textContent = "Refreshing…";

      const headerBtn = $("#header-refresh-btn");
      if (headerBtn) headerBtn.classList.add("spinning");

      try {
        await refreshAll(false);
        toast("Refreshed! ✅");
      } catch (e) {
        console.warn("Pull-to-refresh error:", e);
      } finally {
        isRefreshing = false;
        indicator.classList.remove("refreshing");
        indicator.style.height = "0px";
        if (headerBtn) headerBtn.classList.remove("spinning");
        if (text) text.textContent = "Pull to refresh…";
      }
    } else {
      indicator.classList.remove("pulling", "ready");
      indicator.style.height = "0px";
    }
  }, { passive: true });
}

document.addEventListener("DOMContentLoaded", init);

