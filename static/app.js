/* Pantry Planner — DESKTOP frontend (vanilla JS) for the FastAPI backend.
   Same features and API calls as the mobile PWA; layout is desktop-first:
   left sidebar nav, wide multi-column pages, table inventory editor,
   drag-and-drop photo zones, and a print-friendly shopping view. */
"use strict";

const KEY_STORAGE = "gemini_api_key";
const GROQ_KEY_STORAGE = "groq_api_key";
const OPENROUTER_KEY_STORAGE = "openrouter_api_key";
const HC_STORAGE = "daily_headcounts";

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

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

function setMsg(el, text, kind) {
  el.textContent = text || "";
  el.className = "form-msg" + (kind ? " " + kind : "");
}

/* ---------------- Provider key handling ---------------- */

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

async function validateProviderKey(provider, candidate) {
  try {
    const res = await fetch("/api/validate-provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
function hideSetup() { $("#setup-screen").hidden = true; maybeShowPhoneSetup(); }

function requireKey() {
  if (hasKey()) return true;
  showSetup();
  toast("Add your free Gemini key first 🔑");
  return false;
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
  let body = opts.body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const res = await fetch(path, { method: opts.method || "GET", headers, body });
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const j = await res.json();
      msg = j.detail || msg;
    } catch (e) { /* keep default */ }
    throw new Error(msg);
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

/* ---------------- Inventory (Scan page, table editor) ---------------- */

function inventoryRow(item) {
  const row = document.createElement("tr");
  row.className = "inv-row";
  if (item.id != null) row.dataset.id = item.id;
  const locOpts = LOCATIONS.map(
    (l) => `<option ${l === item.location ? "selected" : ""}>${esc(l)}</option>`).join("");
  const catOpts = CATEGORIES.map(
    (c) => `<option ${c === item.category ? "selected" : ""}>${esc(c)}</option>`).join("");
  row.innerHTML = `
    <td class="col-item"><input class="inv-name" type="text" placeholder="Item name" value="${esc(item.item_name || "")}"></td>
    <td class="col-qty"><input class="inv-qty" type="number" min="0" step="0.5" value="${item.quantity ?? 0}"></td>
    <td class="col-unit"><input class="inv-unit" type="text" placeholder="unit" value="${esc(item.unit || "count")}"></td>
    <td class="col-loc"><select class="inv-loc">${locOpts}</select></td>
    <td class="col-cat"><select class="inv-cat">${catOpts}</select></td>
    <td class="col-del"><button class="inv-del" title="Remove item" aria-label="Remove item">✕</button></td>`;
  row.querySelector(".inv-del").addEventListener("click", () => row.remove());
  return row;
}

function renderInventory() {
  const list = $("#inventory-list");
  list.innerHTML = "";
  state.inventory.forEach((item) => list.appendChild(inventoryRow(item)));
  $("#inventory-empty").style.display = state.inventory.length ? "none" : "block";
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
  try {
    const data = await api("/api/inventory", { method: "PUT", body: collectInventoryRows() });
    state.inventory = data.inventory;
    renderInventory();
    setMsg(msg, "✅ Inventory saved.", "ok");
    toast("Inventory updated!");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

/* ---------------- Diet & settings ---------------- */

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
    wrap.innerHTML = `<label for="hc-${day}">${day}</label>
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
  const body = {
    base_headcount: parseInt($("#base-headcount").value, 10) || 1,
    dietary_tags: Array.from(document.querySelectorAll("#diet-tags input:checked")).map((cb) => cb.value),
    custom_diet_notes: $("#custom-notes").value,
    essential_staples: $("#staples").value,
  };
  try {
    state.settings = await api("/api/settings", { method: "PUT", body });
    collectDailyHeadcounts();
    renderStatus();
    setMsg(msg, "✅ Household dietary preferences saved!", "ok");
    toast("Settings saved!");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

/* ---------------- Menu (week grid) ---------------- */

function mealBlock(emoji, label, meal) {
  if (!meal) return "";
  const steps = (meal.prep_steps || []).map((s) => `<li>${esc(s)}</li>`).join("");
  return `<div class="meal">
    <h4>${emoji} ${label}: ${esc(meal.dish_name || "")}</h4>
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
      ${mealBlock("🌅", "Breakfast", day.breakfast)}
      ${mealBlock("☀️", "Lunch", day.lunch)}
      ${mealBlock("🌙", "Dinner", day.dinner)}
      ${deduct ? `<p class="deduct-note"><b>Inventory to Deduct:</b> ${deduct}</p>` : ""}
      <button class="btn ${day.cooked ? "" : "btn-primary"} cook-btn" ${day.cooked ? "disabled" : ""}>
        ${day.cooked ? `✅ ${esc(day.day)} Cooked &amp; Deducted` : `🍳 Cooked ${esc(day.day)}'s Meals — Subtract from Inventory`}
      </button>`;
    const btn = card.querySelector(".cook-btn");
    if (!day.cooked) btn.addEventListener("click", () => cookDay(idx));
    wrap.appendChild(card);
  });
  renderStatus();
}

async function generatePlan() {
  if (!requireKey()) return;
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
  try {
    const data = await api("/api/plan/cook", { method: "POST", body: { day_index: dayIndex } });
    state.plan = data.plan;
    state.planCreatedAt = data.created_at;
    state.inventory = data.inventory;
    renderMenu();
    renderInventory();
    toast(data.deducted && data.deducted.length
      ? `Subtracted ${data.deducted.length} items from inventory!`
      : "Marked as cooked!");
  } catch (e) {
    toast(e.message);
  }
}

/* ---------------- Shopping ---------------- */

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
  const msg = $("#shop-msg");
  const n = purchasedCount();
  if (n === 0) return;
  if (!confirm(`Remove ${n} purchased item${n === 1 ? "" : "s"} from the list?`)) return;
  try {
    const data = await api("/api/shopping/clear-purchased", { method: "POST" });
    state.plan = data.plan;
    renderShopping();
    setMsg(msg, `✅ Cleared ${data.cleared} purchased item${data.cleared === 1 ? "" : "s"} from the list.`, "ok");
    toast("Purchased items cleared");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

async function restock() {
  const msg = $("#shop-msg");
  try {
    const data = await api("/api/shopping/restock", {
      method: "POST",
      body: { indices: checkedShoppingIndices() },
    });
    state.plan = data.plan;
    state.inventory = data.inventory;
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

/* Print-friendly shopping view: fill #print-area with just the list,
   then let the @media print stylesheet hide everything else. */
function printShoppingList() {
  const area = $("#print-area");
  if (!state.plan) {
    toast("Generate a weekly menu first.");
    return;
  }
  const includeBought = $("#export-include").checked;
  const items = state.plan.shopping_list || [];
  const lines = [
    `<h1>🛒 Shopping List</h1>`,
    `<p class="print-meta">Plan generated: ${esc(state.planCreatedAt || "")}</p>`,
    `<h2>This Week's Menu</h2><ul class="print-menu">`,
  ];
  (state.plan.menu || []).forEach((d) => {
    lines.push(`<li><b>${esc(d.day)}</b> — Breakfast: ${esc(d.breakfast?.dish_name || "")} | ` +
      `Lunch: ${esc(d.lunch?.dish_name || "")} | Dinner: ${esc(d.dinner?.dish_name || "")}</li>`);
  });
  lines.push(`</ul><h2>To Buy</h2>`);
  const cats = [...new Set(items.map((i) => i.category || "General"))].sort();
  let any = false;
  cats.forEach((cat) => {
    const catItems = items.filter((i) =>
      (i.category || "General") === cat && (includeBought || !i.purchased));
    if (!catItems.length) return;
    any = true;
    lines.push(`<h3>${esc(cat)}</h3><ul class="print-items">`);
    catItems.forEach((i) => {
      const box = i.purchased ? "☑" : "☐";
      lines.push(`<li class="${i.purchased ? "bought" : ""}">${box} ${esc(i.item_name)} — ` +
        `${esc(i.amount_needed)} (store to: ${esc(i.default_location || "Cupboard / Pantry")})</li>`);
    });
    lines.push(`</ul>`);
  });
  if (!any) lines.push(`<p>🎉 Nothing to buy — inventory covers the week!</p>`);
  area.innerHTML = lines.join("");
  window.print();
}

/* ---------------- Photo inputs + drag & drop ---------------- */

function wirePhotoInput(fileInput, nameEl, previewEl, btn) {
  fileInput.addEventListener("change", () => {
    const files = fileInput.files;
    if (files && files.length) {
      nameEl.textContent = files.length > 1
        ? `${files.length} photos chosen (${files[0].name}, …)`
        : files[0].name;
      previewEl.src = URL.createObjectURL(files[0]);
      previewEl.hidden = false;
      btn.disabled = false;
    }
  });
}

/* Desktop drag-and-drop: dropping an image anywhere on the zone feeds the
   same hidden file input the click-picker uses, then fires its change event. */
function wireDropZone(zone, fileInput) {
  zone.addEventListener("click", (e) => {
    /* The hidden input lives inside the zone; ignore its own bubbled clicks
       so a programmatic click() doesn't re-trigger the picker in a loop. */
    if (e.target !== fileInput) fileInput.click();
  });
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  ["dragenter", "dragover"].forEach((evt) =>
    zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((evt) =>
    zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove("dragover"); }));
  zone.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    const images = files
      ? [...files].filter((f) => f.type.startsWith("image/"))
      : [];
    if (images.length) {
      const dt = new DataTransfer();
      images.forEach((f) => dt.items.add(f));
      fileInput.files = dt.files;
      fileInput.dispatchEvent(new Event("change"));
    } else {
      toast("Please drop an image file.");
    }
  });
  /* Keep drops meant for the zone from navigating the whole window away. */
  ["dragover", "drop"].forEach((evt) =>
    window.addEventListener(evt, (e) => e.preventDefault()));
}

async function scanShelfPhoto() {
  if (!requireKey()) return;
  const fileInput = $("#scan-file");
  const msg = $("#scan-msg");
  const btn = $("#scan-btn");
  if (!fileInput.files[0]) return;
  btn.disabled = true;
  setMsg(msg, `Scanning ${state.scanLocation} and estimating amounts…`);
  try {
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("location", state.scanLocation);
    const data = await api("/api/scan-photo", { method: "POST", body: fd });
    state.inventory = data.inventory;
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
  renderInventory();
  renderShopping();
  const servedTag = data.served_by ? ` (served by ${esc(data.served_by)})` : "";
  setMsg(msg, `✅ Restocked ${data.logs.length} items from ${data.store_name}${servedTag}:`, "ok");
  // Receipt logs use light markdown (**bold**, *italic*) — render it safely.
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
  const fileInput = $("#receipt-file");
  const msg = $("#receipt-msg");
  const btn = $("#receipt-btn");
  if (!fileInput.files.length) return;
  btn.disabled = true;
  setMsg(msg, "Reading receipt, decoding abbreviations, and updating inventory…");
  try {
    const fd = new FormData();
    for (const f of fileInput.files) fd.append("files", f);
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

/* ---------------- Connect Phone (desktop only; uses the loopback-only /api/desktop/* API) ---------------- */

async function loadPhoneInfo() {
  try {
    state.phone = await api("/api/desktop/phone-access");
  } catch (e) {
    // Not the desktop itself (e.g. this page was opened from a phone): hide the page entirely.
    state.phone = null;
    $("#nav-phone").hidden = true;
    return;
  }
  $("#nav-phone").hidden = false;
  renderPhonePanel();
  maybeShowPhoneSetup();
}

function maybeShowPhoneSetup() {
  // Ask exactly once (phone_access is null until answered); never stack on top of the key setup screen.
  if (state.phone && state.phone.phone_access === null && $("#setup-screen").hidden) {
    $("#phone-setup-screen").hidden = false;
  }
}

function renderPhonePanel() {
  const p = state.phone;
  if (!p) return;
  const on = p.phone_access === true;
  $("#phone-toggle").checked = on;
  let status = on
    ? (p.listening_on_network === false
        ? "Phone access is on, but the server hasn't switched over yet."
        : "Phone access is ON — this PC is listening on your home network.")
    : "Phone access is OFF — only this PC can use the app.";
  if (p.restart_required && !p.can_restart) status += " Close and reopen the app to apply the change.";
  $("#phone-status").textContent = status;

  $("#phone-details").hidden = !on;
  const wrap = $("#phone-urls");
  wrap.innerHTML = "";
  if (on) {
    if (!p.urls.length) {
      wrap.textContent = "No home-network address found. Is this PC connected to Wi-Fi?";
    }
    p.urls.forEach((u) => {
      const box = document.createElement("div");
      box.className = "phone-url";
      box.innerHTML = `
        <img class="qr-img" alt="QR code for ${esc(u)}" src="/api/desktop/qr.svg?url=${encodeURIComponent(u)}">
        <div><code class="url-text">${esc(u)}</code>
        <p class="hint">Type this into the phone app, or scan the code.</p></div>`;
      wrap.appendChild(box);
    });
  }
  $("#phone-code").value = p.access_code || "";
}

async function waitForRestart(oldBootId, maxMs = 25000) {
  const t0 = Date.now();
  await new Promise((r) => setTimeout(r, 700));
  while (Date.now() - t0 < maxMs) {
    try {
      const info = await api("/api/desktop/phone-access");
      // Only accept the NEW server: a different boot id (the old one may still answer while shutting down).
      if (!oldBootId || info.boot_id !== oldBootId) return info;
    } catch (e) { /* server is restarting */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("The server didn't come back. Close and reopen the app.");
}

async function setPhoneAccess(enabled, msgEl) {
  setMsg(msgEl, enabled ? "Turning on phone access…" : "Turning off phone access…");
  const oldBoot = state.phone ? state.phone.boot_id : null;
  try {
    let info = await api("/api/desktop/phone-access", { method: "PUT", body: { enabled } });
    if (info.restarting) {
      setMsg(msgEl, "Restarting the server…");
      info = await waitForRestart(oldBoot);
    }
    state.phone = info;
    renderPhonePanel();
    setMsg(msgEl, enabled ? "✅ Phone access is on." : "✅ Phone access is off.", "ok");
    return true;
  } catch (e) {
    setMsg(msgEl, e.message, "err");
    try { state.phone = await api("/api/desktop/phone-access"); renderPhonePanel(); } catch (e2) { /* ignore */ }
    return false;
  }
}

async function savePhoneCode(code) {
  const msg = $("#phone-code-msg");
  const value = code === null ? null : String(code).trim();
  if (value === "") { setMsg(msg, "Type or generate a code first (or use Remove code).", "err"); return; }
  try {
    state.phone = await api("/api/desktop/phone-access", { method: "PUT", body: { access_code: value } });
    renderPhonePanel();
    setMsg(msg, value === null ? "✅ Access code removed." : "✅ Access code saved. Enter it in the phone app.", "ok");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

function generateAccessCode() {
  const chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"; // no look-alikes (0/O, 1/I)
  const rnd = new Uint32Array(6);
  crypto.getRandomValues(rnd);
  return Array.from(rnd, (n) => chars[n % chars.length]).join("");
}

/* ---------------- Sidebar navigation ---------------- */

function wireNav() {
  document.querySelectorAll(".sidenav button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".sidenav button").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-page").forEach((p) => p.classList.toggle("active", p.id === btn.dataset.tab));
    });
  });
}

/* ---------------- Init ---------------- */

async function refreshAll() {
  const [inv, settings, latest] = await Promise.all([
    api("/api/inventory"),
    api("/api/settings"),
    api("/api/plan/latest"),
  ]);
  state.inventory = inv;
  state.settings = settings;
  state.planId = latest.plan_id;
  state.planCreatedAt = latest.created_at;
  state.plan = latest.plan;
  renderInventory();
  fillDietForm();
  renderMenu();
  renderShopping();
}

function init() {
  buildDietForm();
  wireNav();

  /* Key onboarding: first-run setup screen when nothing is stored. */
  if (!hasKey() && !sessionStorage.getItem("setup_skipped")) showSetup();

  // Populate AI provider key inputs
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

  /* Scan page */
  document.querySelectorAll("#scan-location button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#scan-location button").forEach((b) => b.classList.toggle("active", b === btn));
      state.scanLocation = btn.dataset.loc;
    });
  });
  wirePhotoInput($("#scan-file"), $("#scan-file-name"), $("#scan-preview"), $("#scan-btn"));
  wireDropZone($("#scan-drop"), $("#scan-file"));
  $("#scan-btn").addEventListener("click", scanShelfPhoto);
  $("#inv-add-btn").addEventListener("click", () => {
    $("#inventory-list").appendChild(inventoryRow({
      item_name: "", quantity: 1, unit: "count",
      location: state.scanLocation, category: "Pantry/Dry",
    }));
    $("#inventory-empty").style.display = "none";
  });
  $("#inv-save-btn").addEventListener("click", saveInventory);
  $("#inv-clear-btn").addEventListener("click", async () => {
    if (!confirm("Clear ALL inventory? This cannot be undone.")) return;
    try {
      await api("/api/inventory", { method: "DELETE" });
      state.inventory = [];
      renderInventory();
      toast("Inventory cleared.");
    } catch (e) { toast(e.message); }
  });

  /* Diet page */
  $("#diet-save-btn").addEventListener("click", saveDiet);

  /* Menu page */
  $("#generate-btn").addEventListener("click", generatePlan);

  /* Shopping page */
  $("#restock-btn").addEventListener("click", restock);
  $("#clear-purchased-btn").addEventListener("click", clearPurchased);
  $("#export-btn").addEventListener("click", exportList);
  $("#print-btn").addEventListener("click", printShoppingList);
  wirePhotoInput($("#receipt-file"), $("#receipt-file-name"), $("#receipt-preview"), $("#receipt-btn"));
  wireDropZone($("#receipt-drop"), $("#receipt-file"));
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

  /* Connect Phone page + first-run question (desktop only; hidden if the API says we're not the desktop) */
  $("#phone-toggle").addEventListener("change", (e) => setPhoneAccess(e.target.checked, $("#phone-msg")));
  $("#phone-setup-yes").addEventListener("click", async () => {
    if (await setPhoneAccess(true, $("#phone-setup-msg"))) $("#phone-setup-screen").hidden = true;
  });
  $("#phone-setup-no").addEventListener("click", async () => {
    if (await setPhoneAccess(false, $("#phone-setup-msg"))) $("#phone-setup-screen").hidden = true;
  });
  $("#phone-code-gen").addEventListener("click", () => { $("#phone-code").value = generateAccessCode(); });
  $("#phone-code-save").addEventListener("click", () => savePhoneCode($("#phone-code").value));
  $("#phone-code-clear").addEventListener("click", () => savePhoneCode(null));

  refreshAll().catch((e) => toast(`Could not load data: ${e.message}`));
  loadPhoneInfo();
}

document.addEventListener("DOMContentLoaded", init);
