/**
 * Automated test suite for mobile client app.js logic:
 * - State machine transitions
 * - Offline cache persistence and retrieval
 * - Hand-add outbox queuing and duplicate protection
 * - Destructive action blocking while offline
 * - Server URL and token injection
 */

const assert = require("assert");
const fs = require("fs");
const path = require("path");

// Mock LocalStorage
class LocalStorageMock {
  constructor() {
    this.store = {};
  }
  getItem(key) {
    return this.store[key] !== undefined ? this.store[key] : null;
  }
  setItem(key, value) {
    this.store[key] = String(value);
  }
  removeItem(key) {
    delete this.store[key];
  }
  clear() {
    this.store = {};
  }
}

// Set up global mocks for headless testing
global.localStorage = new LocalStorageMock();
global.sessionStorage = new LocalStorageMock();

// Mock DOM elements
const domElements = {};
function getOrCreateElement(id) {
  if (!domElements[id]) {
    const el = {
      id,
      value: "",
      textContent: "",
      _innerHTML: "",
      className: "",
      classList: {
        classes: new Set(),
        add(c) { this.classes.add(c); },
        remove(c) { this.classes.delete(c); },
        toggle(c, val) { if (val === undefined) val = !this.classes.has(c); if (val) this.add(c); else this.remove(c); },
        contains(c) { return this.classes.has(c); }
      },
      hidden: false,
      readOnly: false,
      style: {},
      disabled: false,
      dataset: {},
      children: [],
      addEventListener(evt, fn) {
        if (!this._listeners) this._listeners = {};
        if (!this._listeners[evt]) this._listeners[evt] = [];
        this._listeners[evt].push(fn);
        if (evt === "click") this.onclick = fn;
      },
      click() {
        if (this.onclick) this.onclick({ preventDefault() {} });
        if (this._listeners && this._listeners["click"]) {
          for (const fn of this._listeners["click"]) fn({ preventDefault() {} });
        }
      },
      setAttribute(attr, val) { this[attr] = val; },
      getAttribute(attr) { return this[attr] !== undefined ? this[attr] : null; },
      hasAttribute(attr) { return this[attr] !== undefined; },
      removeAttribute(attr) { delete this[attr]; },
      appendChild(child) { this.children.push(child); },
      removeChild(child) {
        const idx = this.children.indexOf(child);
        if (idx !== -1) this.children.splice(idx, 1);
      },
      contains(child) { return this.children.includes(child); },
      insertBefore(newNode, refNode) {
        const idx = this.children.indexOf(refNode);
        if (idx !== -1) {
          this.children.splice(idx, 0, newNode);
        } else {
          this.children.push(newNode);
        }
      },
      scrollIntoView() {},
      focus() {},
      closest() { return this; },
      querySelector(s) {
        const matches = this.querySelectorAll(s);
        return matches.length ? matches[0] : getOrCreateElement(this.id + "_child_" + s.replace(/[^a-zA-Z0-9]/g, "_"));
      },
      querySelectorAll(s) {
        const out = [];
        const cleanCls = s.replace(/^\./, "");
        function search(node) {
          if (node.children) {
            for (const ch of node.children) {
              if (s.startsWith(".") && ch.className && ch.className.split(" ").includes(cleanCls)) {
                out.push(ch);
              }
              search(ch);
            }
          }
        }
        search(this);
        return out;
      },
    };
    Object.defineProperty(el, "innerHTML", {
      get() { return this._innerHTML; },
      set(val) {
        this._innerHTML = val;
        if (val === "") {
          this.children = [];
        }
      }
    });
    domElements[id] = el;
  }
  return domElements[id];
}

global.document = {
  getElementById(id) {
    return getOrCreateElement(id);
  },
  querySelector(sel) {
    if (sel.includes(" ")) {
      const parts = sel.split(/\s+/);
      const parent = this.querySelector(parts[0]);
      if (parent && parent.querySelector) {
        return parent.querySelector(parts.slice(1).join(" "));
      }
    }
    if (sel.startsWith("#")) {
      return getOrCreateElement(sel.slice(1));
    }
    const wrap = domElements["menu-days"];
    if (wrap && wrap.children && (sel === ".meal-header" || sel === ".changed-note")) {
      for (const card of wrap.children) {
        if (sel === ".changed-note") {
          const m = (card.innerHTML || "").match(/<div class="changed-note">([\s\S]*?)<\/div>/);
          return {
            innerHTML: m ? m[0] : "",
            textContent: m ? m[1] : "",
          };
        }
        if (card.innerHTML && card.innerHTML.includes(sel.replace(".", ""))) {
          return {
            innerHTML: card.innerHTML,
            textContent: card.innerHTML,
            querySelectorAll() { return []; }
          };
        }
      }
    }
    const all = this.querySelectorAll(sel);
    if (all.length) return all[0];
    return getOrCreateElement("mock_" + sel.replace(/[^a-zA-Z0-9]/g, "_"));
  },
  querySelectorAll(sel) {
    const onHand = domElements["on-hand-groups"];
    if (onHand && (sel === ".onhand-group" || sel.startsWith(".onhand-group["))) {
      const results = [];
      for (const ch of (onHand.children || [])) {
        if (sel.includes("[data-location=")) {
          const m = sel.match(/\[data-location="([^"]+)"\]/);
          if (m && ch.dataset && ch.dataset.location === m[1]) results.push(ch);
        } else {
          results.push(ch);
        }
      }
      return results;
    }
    const results = [];
    const cleanSel = sel.replace(/^\./, "");
    for (const key of Object.keys(domElements)) {
      const el = domElements[key];
      if (sel.startsWith(".") && el.className && el.className.split(" ").includes(cleanSel)) {
        results.push(el);
      } else if (sel.includes("[data-location=")) {
        const m = sel.match(/\[data-location="([^"]+)"\]/);
        if (m && el.dataset && el.dataset.location === m[1]) {
          results.push(el);
        }
      }
    }
    return results;
  },
  createElement(tag) {
    const el = getOrCreateElement("tag_" + tag + "_" + Math.random());
    el.tagName = tag.toUpperCase();
    return el;
  },
  addEventListener(evt, fn) {
    if (!this._listeners) this._listeners = {};
    if (!this._listeners[evt]) this._listeners[evt] = [];
    this._listeners[evt].push(fn);
  },
  visibilityState: "visible"
};

global.window = {
  addEventListener() {},
  scrollTo() {},
  open() {},
  location: { protocol: "http:", origin: "http://localhost:8000", pathname: "/", search: "", hash: "" },
  history: {
    replaceState(st, title, url) {
      if (typeof url === "string") {
        try {
          const u = new URL(url, "http://localhost:8000");
          global.window.location.search = u.search;
          global.window.location.pathname = u.pathname;
          global.window.location.hash = u.hash;
        } catch (e) {
          const qIdx = url.indexOf("?");
          global.window.location.search = qIdx !== -1 ? url.substring(qIdx) : "";
        }
      }
    }
  },
  confirm(msg) {
    return true;
  }
};


// Mock fetch
let fetchHandler = null;
global.fetch = async (url, opts = {}) => {
  if (fetchHandler) {
    return fetchHandler(url, opts);
  }
  return {
    ok: true,
    status: 200,
    headers: { get: (h) => (h === "content-type" ? "application/json" : "") },
    json: async () => ({ status: "ok", app: "pantry-planner" }),
    text: async () => JSON.stringify({ status: "ok" })
  };
};

const vm = require("vm");
const appJsPath = fs.existsSync(path.resolve(__dirname, "../api/static/app.js"))
  ? path.resolve(__dirname, "../api/static/app.js")
  : path.resolve(__dirname, "../static/app.js");
const appJsCode = fs.readFileSync(appJsPath, "utf-8");

const indexHtmlPath = fs.existsSync(path.resolve(__dirname, "../api/static/index.html"))
  ? path.resolve(__dirname, "../api/static/index.html")
  : path.resolve(__dirname, "../static/index.html");

const mealImagesDirPath = fs.existsSync(path.resolve(__dirname, "../api/static/meal-images"))
  ? path.resolve(__dirname, "../api/static/meal-images")
  : path.resolve(__dirname, "../static/meal-images");

// Evaluate in global context
vm.runInThisContext(appJsCode);

// Test suite execution
async function runTests() {
  console.log("Starting Mobile Client unit tests...");
  let passed = 0;

  // Test 1: buildUrl
  localStorage.clear();
  assert.strictEqual(buildUrl("/api/inventory"), "/api/inventory", "Relative URL when no server stored");
  localStorage.setItem(SERVER_URL_STORAGE, "http://192.168.4.159:8000");
  assert.strictEqual(buildUrl("/api/inventory"), "http://192.168.4.159:8000/api/inventory", "Prefixed with desktop server URL");
  assert.strictEqual(buildUrl("api/inventory"), "http://192.168.4.159:8000/api/inventory", "Handles leading slash gracefully");
  console.log("✓ Test 1 Passed: buildUrl resolution");
  passed++;

  // Test 2: api() headers injection
  let capturedHeaders = null;
  localStorage.setItem(KEY_STORAGE, "test-gemini-key-123");
  localStorage.setItem(ACCESS_TOKEN_STORAGE, "secret-token-456");
  fetchHandler = async (url, opts) => {
    capturedHeaders = opts.headers;
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => [{ id: 1, item_name: "Test Apple" }]
    };
  };
  const data = await api("/api/inventory");
  assert.strictEqual(capturedHeaders["X-Gemini-Key"], "test-gemini-key-123");
  assert.strictEqual(capturedHeaders["X-Pantry-Token"], "secret-token-456");
  assert.strictEqual(data.length, 1);
  console.log("✓ Test 2 Passed: api() injects Gemini Key and Pantry Access Token");
  passed++;

  // Test 3: Connection State transitions
  setConnectionState("CONNECTED");
  assert.strictEqual(isOnline(), true);
  assert.strictEqual(document.querySelector("#connection-banner").hidden, true);
  assert.strictEqual(document.querySelector("#server-status-dot").classList.contains("connected"), true);

  setConnectionState("DISCONNECTED");
  assert.strictEqual(isOnline(), false);
  assert.strictEqual(document.querySelector("#connection-banner").hidden, false);
  assert.strictEqual(document.querySelector("#server-status-dot").classList.contains("disconnected"), true);

  setConnectionState("AUTH_REQUIRED");
  assert.strictEqual(isOnline(), false);
  assert.strictEqual(document.querySelector("#connection-banner").hidden, false);
  assert.strictEqual(document.querySelector("#server-status-dot").classList.contains("auth"), true);
  console.log("✓ Test 3 Passed: Connection state machine UI updates");
  passed++;

  // Test 4: Offline cache save & load
  const mockInv = [{ id: 10, item_name: "Carrots", quantity: 5, unit: "count" }];
  const mockSettings = { base_headcount: 4, dietary_tags: ["Gluten-Free"] };
  const mockPlan = { plan_id: 1, created_at: "2026-10-16 12:00", plan: { menu: [] } };
  saveToOfflineCache(mockInv, mockSettings, mockPlan);

  // Clear memory state
  state.inventory = [];
  state.settings = null;
  state.plan = null;

  loadFromOfflineCache();
  assert.strictEqual(state.inventory.length, 1);
  assert.strictEqual(state.inventory[0].item_name, "Carrots");
  assert.strictEqual(state.settings.base_headcount, 4);
  assert.strictEqual(state.planId, 1);
  console.log("✓ Test 4 Passed: Offline cache save and load");
  passed++;

  // Test 5: Safe Outbox queuing when offline
  setConnectionState("DISCONNECTED");
  localStorage.removeItem(OUTBOX_STORAGE);
  const newItem = {
    item_name: "Fresh Spinach",
    quantity: 2,
    unit: "bags",
    location: "Fridge",
    category: "Produce/Veggies"
  };
  queueOfflineAdd(newItem);

  const outbox = getOutbox();
  assert.strictEqual(outbox.length, 1);
  assert.strictEqual(outbox[0].item_name, "Fresh Spinach");
  assert.ok(outbox[0].client_id.startsWith("outbox_"));
  assert.strictEqual(state.inventory[0].item_name, "Fresh Spinach");
  assert.strictEqual(state.inventory[0]._isQueued, true);
  console.log("✓ Test 5 Passed: queueOfflineAdd appends to outbox and local inventory");
  passed++;

  // Test 6: Outbox flushing and deduplication
  let postedItems = [];
  fetchHandler = async (url, opts) => {
    try {
      if (opts.method === "POST" && url.includes("/api/inventory")) {
        const body = JSON.parse(opts.body);
        postedItems.push(...body.items);
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => ({ added: body.items.length, inventory: [] })
        };
      }
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => []
      };
    } catch (err) {
      console.error("fetchHandler error:", err);
      throw err;
    }
  };

  setConnectionState("CONNECTED");
  // Allow the auto-flush microtask from state change to complete
  await new Promise((r) => setTimeout(r, 20));

  assert.strictEqual(postedItems.length, 1);
  assert.strictEqual(postedItems[0].item_name, "Fresh Spinach");
  assert.strictEqual(getOutbox().length, 0, "Outbox should be empty after successful flush");

  // Re-flush with same outbox item to verify deduplication
  localStorage.setItem(OUTBOX_STORAGE, JSON.stringify([outbox[0]]));
  postedItems = [];
  await flushOutbox();
  assert.strictEqual(postedItems.length, 0, "Deduplication prevented re-sending already synced item");
  console.log("✓ Test 6 Passed: Outbox flush and deduplication");
  passed++;

  // Test 7: Destructive actions blocked while offline
  setConnectionState("DISCONNECTED");
  let attemptedFetch = false;
  fetchHandler = async () => {
    attemptedFetch = true;
    return { ok: true, status: 200, json: async () => ({}) };
  };

  attemptedFetch = false;
  await saveInventory();
  assert.strictEqual(attemptedFetch, false, "saveInventory blocked while offline");

  attemptedFetch = false;
  await generatePlan();
  assert.strictEqual(attemptedFetch, false, "generatePlan blocked while offline");

  attemptedFetch = false;
  await cookDay(0);
  assert.strictEqual(attemptedFetch, false, "cookDay blocked while offline");

  attemptedFetch = false;
  await restock();
  assert.strictEqual(attemptedFetch, false, "restock blocked while offline");

  attemptedFetch = false;
  await clearPurchased();
  assert.strictEqual(attemptedFetch, false, "clearPurchased blocked while offline");

  attemptedFetch = false;
  await scanShelfPhoto();
  assert.strictEqual(attemptedFetch, false, "scanShelfPhoto blocked while offline");
  console.log("✓ Test 7 Passed: All destructive actions safely blocked while offline");
  passed++;

  // Test 8: Key ownership rule: connected mode never blocks on client keys & hides standalone key UI
  setConnectionState("CONNECTED");
  localStorage.removeItem(KEY_STORAGE);
  localStorage.removeItem(GROQ_KEY_STORAGE);
  localStorage.removeItem(OPENROUTER_KEY_STORAGE);
  assert.strictEqual(hasKey(), false, "Client has no local keys stored");
  assert.strictEqual(requireKey(), true, "Connected mode permits AI operations without client keys");
  assert.strictEqual(document.querySelector("#standalone-keys-section").hidden, true, "Standalone keys UI hidden when connected");
  assert.strictEqual(document.querySelector("#connected-keys-notice").hidden, false, "Connected AI notice visible when connected");

  // In standalone mode (no server URL), standalone keys UI is shown
  localStorage.removeItem(SERVER_URL_STORAGE);
  setConnectionState("DISCONNECTED");
  assert.strictEqual(document.querySelector("#standalone-keys-section").hidden, false, "Standalone keys UI shown in standalone mode");
  assert.strictEqual(document.querySelector("#connected-keys-notice").hidden, true, "Connected AI notice hidden in standalone mode");

  console.log("✓ Test 8 Passed: Connected mode never prompts for or blocks on client keys and manages key UI visibility");
  passed++;

  // Test 9: Key persistence in localStorage survives across simulated restarts
  localStorage.setItem(KEY_STORAGE, "AIzaSyTestGeminiKey123");
  localStorage.setItem(GROQ_KEY_STORAGE, "gsk_TestGroqKey456");
  localStorage.setItem(OPENROUTER_KEY_STORAGE, "sk-or-v1-TestOpenRouter789");
  assert.strictEqual(storedKey(), "AIzaSyTestGeminiKey123");
  assert.strictEqual(storedGroqKey(), "gsk_TestGroqKey456");
  assert.strictEqual(storedOpenRouterKey(), "sk-or-v1-TestOpenRouter789");
  console.log("✓ Test 9 Passed: Provider keys persist reliably across storage accesses");
  passed++;

  // Test 10: Clean first-run state: empty shopping list displays plain empty message
  state.plan = null;
  state.inventory = [];
  renderShopping();
  const emptyEl = document.querySelector("#shopping-empty");
  assert.strictEqual(emptyEl.style.display, "block");
  assert.strictEqual(
    emptyEl.textContent,
    "No menu planned yet — your shopping list will build from your plan."
  );
  assert.strictEqual(document.querySelectorAll("#shopping-list .shop-item").length, 0);
  console.log("✓ Test 10 Passed: Clean first-run shopping list is empty with exact required guidance message");
  // Test 11: Meal swap rendering: changed_from note renders and clears on full re-plan
  state.plan = {
    menu: [
      {
        day: "Monday",
        headcount: 2,
        breakfast: { dish_name: "Tofu Scramble", changed_from: "Old Scramble", prep_steps: ["Scramble tofu"], cook_time_minutes: 10 },
        lunch: null,
        dinner: null,
        ingredients_to_deduct: [],
        cooked: false,
      }
    ],
    shopping_list: []
  };
  renderMenu();
  const mealHeader = document.querySelector(".meal-header");
  assert.ok(mealHeader, "Meal header renders");
  const changedNote = document.querySelector(".changed-note");
  assert.ok(changedNote, "Changed from note renders");
  assert.strictEqual(changedNote.innerHTML.includes("Old Scramble"), true, "Note shows previous meal");

  // On full re-plan, meal without changed_from clears note
  state.plan.menu[0].breakfast = { dish_name: "Fresh Omelet", prep_steps: ["Cook"], cook_time_minutes: 5 };
  renderMenu();
  const clearedNote = document.querySelector(".changed-note");
  assert.strictEqual(clearedNote.innerHTML, "", "Changed from note cleared on new meal");
  console.log("✓ Test 11 Passed: Meal swap Changed-from note renders and clears on re-plan");
  passed++;

  // Test 12: Pull-to-refresh indicator and header refresh button exist
  const headerRefreshBtn = document.querySelector("#header-refresh-btn");
  assert.ok(headerRefreshBtn, "Header refresh button exists");
  const ptrIndicator = document.querySelector("#ptr-indicator");
  assert.ok(ptrIndicator, "Pull-to-refresh indicator exists");
  assert.strictEqual(typeof setupPullToRefresh, "function", "setupPullToRefresh is defined");
  console.log("✓ Test 12 Passed: Pull-to-refresh indicator and header refresh controls present");
  passed++;

  // Test 13: Dedicated "On Hand" inventory view
  state.inventory = [
    { id: 1, item_name: "Zucchini", quantity: 2, unit: "count", location: "Fridge", category: "Produce/Veggies" },
    { id: 2, item_name: "Apples", quantity: 4, unit: "count", location: "Fridge", category: "Produce/Veggies" },
    { id: 3, item_name: "Frozen Peas", quantity: 0, unit: "bags", location: "Freezer", category: "Frozen" },
    { id: 4, item_name: "Brown Rice", quantity: 1, unit: "bag", location: "Cupboard / Pantry", category: "Pantry/Dry" },
  ];
  renderInventory();

  // (a) Check grouping by Fridge, Freezer, Cupboard / Pantry
  // (a) Check grouping by Fridge, Freezer, Cupboard / Pantry
  const groups = document.querySelectorAll(".onhand-group");
  assert.strictEqual(groups.length, 3, "Three on-hand groups render for 3 distinct locations");

  // (b) Check alphabetical order inside Fridge: Apples (A) before Zucchini (Z)
  const fridgeGroup = document.querySelector('.onhand-group[data-location="Fridge"]');
  assert.ok(fridgeGroup, "Fridge group exists");
  const listDiv = fridgeGroup.children.find((c) => c.className === "onhand-list");
  assert.ok(listDiv, "Fridge list container exists");
  assert.ok(listDiv.children[0].innerHTML.includes("Apples"), "First fridge item is Apples (alphabetical)");
  assert.ok(listDiv.children[1].innerHTML.includes("Zucchini"), "Second fridge item is Zucchini (alphabetical)");

  // (c) Check 0 quantity displays as "out"
  const freezerGroup = document.querySelector('.onhand-group[data-location="Freezer"]');
  assert.ok(freezerGroup, "Freezer group exists");
  const freezerList = freezerGroup.children.find((c) => c.className === "onhand-list");
  assert.ok(freezerList, "Freezer list container exists");
  assert.ok(freezerList.children[0].innerHTML.includes('class="onhand-qty out">out<'), "0 quantity renders with out class and 'out' text");

  // (d) Mode toggling & jumpToInventoryEditor
  setInventoryViewMode("onhand");
  assert.strictEqual(document.querySelector("#on-hand-container").hidden, false);
  assert.strictEqual(document.querySelector("#inventory-editor-card").hidden, true);

  jumpToInventoryEditor("Apples");
  assert.strictEqual(document.querySelector("#on-hand-container").hidden, true);
  assert.strictEqual(document.querySelector("#inventory-editor-card").hidden, false);

  console.log("✓ Test 13 Passed: Dedicated On Hand view (Fridge/Freezer/Pantry grouping, alphabetical, 'out' badge, jump-to-editor)");
  passed++;

  // Test 14: Clear menu button, exact confirmation copy, and plan clearing in connected & standalone modes
  const clearTestPlan = {
    menu: [
      {
        day: "Monday",
        headcount: 2,
        breakfast: { dish_name: "Oatmeal", changed_from: "Pancakes" },
        lunch: { dish_name: "Salad" },
        dinner: { dish_name: "Pasta" },
        cooked: false,
      }
    ],
    shopping_list: [{ item_name: "Oats", quantity: 1, unit: "can", category: "Pantry/Dry" }],
    nutrition_notes: "Whole foods",
  };
  state.plan = clearTestPlan;
  state.planId = 42;
  state.planCreatedAt = "2026-10-18 10:00";
  state.inventory = [{ id: 1, item_name: "Milk", quantity: 2, unit: "bottle", location: "Fridge" }];
  state.settings = { base_headcount: 2, dietary_tags: ["Balanced"] };

  // 1. Render plan -> Clear menu button must be visible
  renderMenu();
  const clearBtn = document.querySelector("#clear-menu-btn");
  assert.ok(clearBtn, "Clear menu button exists in DOM");
  assert.strictEqual(clearBtn.style.display, "inline-block", "Clear menu button is visible when a plan exists");

  // 2. Open confirmation modal -> check exact required copy from index.html and element wiring
  const indexHtml = fs.readFileSync(indexHtmlPath, "utf8");
  assert.ok(indexHtml.includes("Start over with a fresh menu?"), "Modal title matches exact required copy");
  assert.ok(
    indexHtml.includes("This clears your current 7-day menu and the shopping list built from it. Your stocked inventory, receipts, and settings stay exactly as they are."),
    "Modal body matches exact required copy"
  );
  assert.ok(indexHtml.includes(">Keep my menu</button>"), "Cancel button matches exact required copy");
  assert.ok(indexHtml.includes(">Clear it</button>"), "Confirm button matches exact required copy");

  showClearMenuModal();
  const clearModal = document.querySelector("#clear-menu-modal");
  assert.strictEqual(clearModal.hidden, false, "Clear menu modal is displayed");

  // 3. Cancel leaves plan completely untouched
  hideClearMenuModal();
  assert.strictEqual(clearModal.hidden, true, "Modal closed on cancel");
  assert.strictEqual(state.plan, clearTestPlan, "Plan left untouched on cancel");
  assert.strictEqual(state.inventory.length, 1, "Inventory untouched on cancel");

  // 4. Confirm in connected mode deletes plan on server and clears view
  setConnectionState("CONNECTED");
  let serverDeleteCalled = false;
  fetchHandler = async (url, opts) => {
    if (url.endsWith("/api/plan") && opts.method === "DELETE") {
      serverDeleteCalled = true;
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ plan_id: null, created_at: null, plan: null })
      };
    }
    return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
  };

  await confirmClearMenu();
  assert.strictEqual(serverDeleteCalled, true, "DELETE /api/plan called on server in connected mode");
  assert.strictEqual(state.plan, null, "state.plan is null after clearing");
  assert.strictEqual(state.planId, null, "state.planId is null after clearing");
  assert.strictEqual(clearBtn.style.display, "none", "Clear menu button hidden when no plan exists");
  assert.strictEqual(document.querySelector("#menu-empty").style.display, "block", "Menu empty state renders");
  assert.strictEqual(
    document.querySelector("#shopping-empty").textContent,
    "No menu planned yet — your shopping list will build from your plan.",
    "Shopping list returns to clean empty state guidance"
  );
  // Verify inventory & settings were NEVER touched
  assert.strictEqual(state.inventory.length, 1, "Inventory preserved untouched");
  assert.strictEqual(state.inventory[0].item_name, "Milk", "Inventory item preserved untouched");
  assert.strictEqual(state.settings.base_headcount, 2, "Settings preserved untouched");

  // 5. Standalone mode clear
  setConnectionState("DISCONNECTED");
  state.plan = clearTestPlan;
  state.planId = 99;
  renderMenu();
  assert.strictEqual(clearBtn.style.display, "inline-block");
  await confirmClearMenu();
  assert.strictEqual(state.plan, null, "Standalone mode clears local plan");
  assert.strictEqual(clearBtn.style.display, "none", "Clear menu button hidden after standalone clear");

  console.log("✓ Test 14 Passed: Clear menu button, confirmation copy, plan deletion, and inventory/settings isolation");
  passed++;

  // Test 15: Milestone 6 - "Shop this list" grocery handoff, Phase 1
  // 1. URL construction unit tests covering both providers with tricky item names
  const icUrl1 = buildGrocerySearchUrl("instacart", "Whole Milk");
  assert.strictEqual(icUrl1, "https://www.instacart.com/store/s?k=Whole%20Milk", "Instacart normal query");

  const icUrl2 = buildGrocerySearchUrl("instacart", "Salt & Pepper", "78701");
  assert.strictEqual(icUrl2, "https://www.instacart.com/store/s?k=Salt%20%26%20Pepper&postal_code=78701", "Instacart ampersand and zip");

  const icUrl3 = buildGrocerySearchUrl("instacart", "1/2 lb Chicken Breasts (boneless)");
  assert.strictEqual(icUrl3, "https://www.instacart.com/store/s?k=1%2F2%20lb%20Chicken%20Breasts%20(boneless)", "Instacart punctuation and slashes");

  const wmUrl1 = buildGrocerySearchUrl("walmart", "Fresh Garlic Cloves");
  assert.strictEqual(wmUrl1, "https://www.walmart.com/search?q=Fresh%20Garlic%20Cloves", "Walmart spaces query");

  const wmUrl2 = buildGrocerySearchUrl("walmart", "Mac & Cheese", "90210");
  assert.strictEqual(wmUrl2, "https://www.walmart.com/search?q=Mac%20%26%20Cheese&postal_code=90210", "Walmart ampersand and zip");

  const wmUrl3 = buildGrocerySearchUrl("walmart", "2% Reduced Fat Milk (1 gal)");
  assert.strictEqual(wmUrl3, "https://www.walmart.com/search?q=2%25%20Reduced%20Fat%20Milk%20(1%20gal)", "Walmart percent and parentheses");

  assert.strictEqual(buildGrocerySearchUrl("walmart", "   "), "", "Empty item name returns empty string");
  assert.throws(() => buildGrocerySearchUrl("unsupported_store", "Eggs"), /Unsupported grocery provider/);

  // 2. Settings persist / clear tests
  localStorage.removeItem(GROCERY_STORE_STORAGE);
  localStorage.removeItem(GROCERY_ZIP_STORAGE);
  assert.strictEqual(getStoredGroceryStore(), "", "Default store is unset initially");
  assert.strictEqual(getStoredGroceryZip(), "", "Default zip is unset initially");

  setStoredGroceryStore("instacart");
  setStoredGroceryZip("78701");
  assert.strictEqual(getStoredGroceryStore(), "instacart", "Persisted instacart store");
  assert.strictEqual(getStoredGroceryZip(), "78701", "Persisted zip");

  setStoredGroceryStore("walmart");
  assert.strictEqual(getStoredGroceryStore(), "walmart", "Round-trip update to walmart persists");

  clearGrocerySettings();
  assert.strictEqual(getStoredGroceryStore(), "", "Clearing settings resets store");
  assert.strictEqual(getStoredGroceryZip(), "", "Clearing settings resets zip");

  // 3. One-off ZIP override does NOT overwrite the saved home store ZIP
  setStoredGroceryStore("walmart");
  setStoredGroceryZip("10001");

  showShopModal();
  const shopModalEl = document.querySelector("#shop-modal");
  assert.strictEqual(shopModalEl.hidden, false, "Shop modal opens");

  const zipInputEl = document.querySelector("#shop-override-zip");
  assert.strictEqual(zipInputEl.value, "10001", "Modal defaults to saved home ZIP");

  zipInputEl.value = "90210";

  let openedUrls = [];
  const origWindowOpen = global.window.open;
  global.window.open = (url) => { openedUrls.push(url); return true; };

  state.plan = {
    shopping_list: [
      { item_name: "Fresh Spinach", amount_needed: "1 bag", purchased: false },
      { item_name: "Olive oil & vinegar", amount_needed: "1 bottle", purchased: false }
    ]
  };

  executeShopHandoff();

  assert.strictEqual(getStoredGroceryZip(), "10001", "One-off ZIP did NOT mutate saved home store ZIP");
  assert.strictEqual(localStorage.getItem(GROCERY_ZIP_STORAGE), "10001", "Storage untouched by one-off override");

  // 4. Empty shopping list disables or hides the button
  state.plan = null;
  renderShopping();
  const shopBtn = document.querySelector("#shop-list-btn");
  assert.strictEqual(shopBtn.disabled, true, "Shop button disabled when no plan exists");
  assert.strictEqual(shopBtn.style.display, "none", "Shop button hidden when no plan exists");

  state.plan = { shopping_list: [] };
  renderShopping();
  assert.strictEqual(shopBtn.disabled, true, "Shop button disabled when shopping list is empty");
  assert.strictEqual(shopBtn.style.display, "none", "Shop button hidden when shopping list is empty");

  state.plan = { shopping_list: [{ item_name: "Bread", amount_needed: "1 loaf", purchased: true }] };
  renderShopping();
  assert.strictEqual(shopBtn.disabled, true, "Shop button disabled when all items are purchased");
  assert.strictEqual(shopBtn.style.display, "inline-block", "Shop button shown when items present");

  state.plan = { shopping_list: [{ item_name: "Bread", amount_needed: "1 loaf", purchased: false }] };
  renderShopping();
  assert.strictEqual(shopBtn.disabled, false, "Shop button enabled when unpurchased items exist");
  assert.strictEqual(shopBtn.style.display, "inline-block", "Shop button shown when items present");

  // 5. Transparency / honesty copy verification
  const rawHtml = fs.readFileSync(indexHtmlPath, "utf-8");
  assert.ok(
    rawHtml.includes("Pantry Planner prepares your shopping search links and hands them over to your chosen grocery service. All ordering, fulfillment, and payment happen directly on the store's website or app."),
    "Exact transparency and honesty copy present in shop modal HTML"
  );
  assert.ok(!rawHtml.includes("affiliate"), "No affiliate references");

  global.window.open = origWindowOpen;

  console.log("✓ Test 15 Passed: Milestone 6 - Shop this list grocery handoff, URL construction, ZIP isolation, and settings persistence");
  passed++;

  // Test 16: Meal images on recipe cards (Milestone 6 Task 3)
  // 1. Mapped category images
  const testCategories = [
    { name: "Fluffy Buttermilk Pancakes", cat: "breakfast" },
    { name: "Crispy Beef Tacos", cat: "tacos" },
    { name: "Spaghetti Bolognese", cat: "pasta" },
    { name: "Chicken and Veggie Stir-Fry", cat: "stir-fry" },
    { name: "Pan-Seared Salmon Fillet", cat: "seafood" },
    { name: "Herb Roasted Chicken Thighs", cat: "roast-chicken" },
    { name: "Creamy Tomato Soup", cat: "soup" },
    { name: "Fresh Garden Salad with Greens", cat: "salad" },
    { name: "Bacon Avocado Burger", cat: "sandwich" }
  ];

  testCategories.forEach(({ name, cat }) => {
    const meal = { dish_name: name, prep_steps: ["Prepare and cook"] };
    assert.strictEqual(getMealImageCategory(meal), cat, `Category matches for ${name}`);
    assert.strictEqual(getMealImageUrl(meal), `meal-images/${cat}.svg`, `Image URL matches for ${name}`);
    const block = mealBlock("🍽️", "Dinner", "dinner", meal, 0, false);
    assert.ok(block.includes(`src="meal-images/${cat}.svg"`), `Meal card renders category image for ${name}`);
    assert.ok(block.includes(`data-category="${cat}"`), `Category attribute present for ${name}`);
    assert.ok(block.includes('class="meal-banner"'), "Meal banner container present");
  });

  // 2. Unmapped meal renders default
  const unmappedMeal = { dish_name: "Grandma's Mysterious Surprise", prep_steps: ["Heat and enjoy"] };
  assert.strictEqual(getMealImageCategory(unmappedMeal), "default", "Unmapped meal resolves to default category");
  assert.strictEqual(getMealImageUrl(unmappedMeal), "meal-images/default.svg", "Unmapped meal resolves to default.svg");
  const unmappedBlock = mealBlock("🌙", "Dinner", "dinner", unmappedMeal, 0, false);
  assert.ok(unmappedBlock.includes('src="meal-images/default.svg"'), "Unmapped meal renders default image");
  assert.ok(unmappedBlock.includes('data-category="default"'), "Unmapped category is default");

  // 3. Image load failure fallback to branded placeholder tile with initial
  assert.ok(unmappedBlock.includes('class="meal-image-placeholder"'), "Placeholder element exists");
  assert.ok(unmappedBlock.includes('<span class="placeholder-initial">G</span>'), "Placeholder has correct dish initial");
  assert.ok(unmappedBlock.includes('onerror="this.style.display=\'none\'; if (this.nextElementSibling) this.nextElementSibling.style.display=\'flex\';"'), "Image onerror switches to placeholder");

  // 4. Swapped meal updates image dynamically
  const initialDish = { dish_name: "Oatmeal with Apples", prep_steps: ["Boil oats"] };
  const initialRender = mealBlock("🌅", "Breakfast", "breakfast", initialDish, 0, false);
  assert.ok(initialRender.includes('src="meal-images/breakfast.svg"'), "Initial meal has breakfast image");

  const swappedDish = { dish_name: "Chicken Enchiladas", changed_from: "Oatmeal with Apples", prep_steps: ["Bake"] };
  const swappedRender = mealBlock("🌅", "Breakfast", "breakfast", swappedDish, 0, false);
  assert.ok(swappedRender.includes('src="meal-images/tacos.svg"'), "Swapped meal updates image to tacos");
  assert.ok(swappedRender.includes('Changed from: <i>Oatmeal with Apples</i>'), "Changed-from note preserved on swapped meal");

  // 5. Verify local bundled image set exists on disk and total size is under 500KB
  const imgDir = mealImagesDirPath;
  const expectedFiles = [
    "tacos.svg", "pasta.svg", "stir-fry.svg", "roast-chicken.svg",
    "salad.svg", "soup.svg", "breakfast.svg", "seafood.svg",
    "sandwich.svg", "default.svg"
  ];
  let totalBytes = 0;
  for (const f of expectedFiles) {
    const fPath = path.join(imgDir, f);
    assert.ok(fs.existsSync(fPath), `Image file ${f} exists`);
    totalBytes += fs.statSync(fPath).size;
  }
  assert.ok(totalBytes > 0 && totalBytes < 500 * 1024, `Total size ${totalBytes} bytes is well within budget (<500KB)`);

  // 6. Verify credits note in HTML
  assert.ok(
    rawHtml.includes("Meal Imagery Credits:</b> Built-in vector culinary illustrations designed specifically for Pantry Planner under Creative Commons CC0 / Open License."),
    "Attribution / credits note present in HTML"
  );

  console.log("✓ Test 16 Passed: Meal images on recipe cards, category mapping, default fallback, placeholder initial, and image swapping");
  passed++;

  // Test 17: Milestone 7 Part 2 — Mom-proof onboarding & magic invite links
  // 1. Single config point HOSTED_DEFAULT_SERVER_URL is empty in A2
  assert.strictEqual(isHostedDefaultConfigured(), false, "HOSTED_DEFAULT_SERVER_URL is empty in A2");
  assert.strictEqual(storedServerUrl(), "", "storedServerUrl() defaults to empty when unconfigured");

  // 2. When configured (simulated Phase B): connect experience collapses to household code, URL in Advanced
  setHostedDefaultServerUrl("https://pantry-space.hf.space");
  assert.strictEqual(isHostedDefaultConfigured(), true, "HOSTED_DEFAULT_SERVER_URL is configured");
  assert.strictEqual(storedServerUrl(), "https://pantry-space.hf.space", "storedServerUrl() falls back to hosted default");

  updateServerSetupScreenLayout();
  const titleEl = document.querySelector("#setup-server-title");
  const descEl = document.querySelector("#setup-server-desc");
  const advEl = document.querySelector("#setup-server-advanced");
  const urlGroupEl = document.querySelector("#setup-server-url-group");
  assert.strictEqual(titleEl.textContent, "Join Your Kitchen", "Title collapses to 'Join Your Kitchen'");
  assert.strictEqual(descEl.textContent, "Enter your household code to connect to your shared pantry.", "Desc reflects household code connect");
  assert.strictEqual(advEl.hidden, false, "Advanced toggle is exposed");
  assert.ok(advEl.contains(urlGroupEl), "Server URL field tucked into Advanced section");

  // Unconfigured (LAN/dev/A2) restores standard layout
  setHostedDefaultServerUrl("");
  updateServerSetupScreenLayout();
  assert.strictEqual(titleEl.textContent, "Connect to Server", "Title restored to 'Connect to Server'");
  assert.strictEqual(advEl.hidden, true, "Advanced toggle is hidden in LAN/dev mode");

  // 3. Magic invite link: consumed once, stored, URL cleaned via history replace
  localStorage.clear();
  global.window.location.search = "?household=Maple+River+42";
  global.window.location.pathname = "/";
  handleMagicInviteLink();
  assert.strictEqual(localStorage.getItem(ACCESS_TOKEN_STORAGE), "Maple River 42", "Household code stored from magic link");
  assert.strictEqual(global.window.location.search, "", "URL query parameter deleted and cleaned");

  // Re-running handleMagicInviteLink with cleaned URL is safe / no-op
  handleMagicInviteLink();
  assert.strictEqual(localStorage.getItem(ACCESS_TOKEN_STORAGE), "Maple River 42", "Code remains intact on subsequent runs");

  // Absent parameter leaves existing storage and URL untouched
  localStorage.setItem(ACCESS_TOKEN_STORAGE, "River Bend 7");
  global.window.location.search = "";
  handleMagicInviteLink();
  assert.strictEqual(localStorage.getItem(ACCESS_TOKEN_STORAGE), "River Bend 7", "Absent parameter leaves stored code untouched");
  assert.strictEqual(global.window.location.search, "", "URL remains untouched");

  // Magic link carrying DIFFERENT code triggers wrong-kitchen guard
  const switchModal = document.querySelector("#switch-kitchen-modal");
  global.window.location.search = "?household=Maple+River+42";
  handleMagicInviteLink();
  assert.strictEqual(switchModal.hidden, false, "Switch kitchen dialog shown when different code in magic link");
  const cancelBtn = document.querySelector("#switch-kitchen-cancel-btn");
  cancelBtn.onclick(); // Click "Stay in my kitchen"
  assert.strictEqual(switchModal.hidden, true, "Modal closed after cancel");
  assert.strictEqual(localStorage.getItem(ACCESS_TOKEN_STORAGE), "River Bend 7", "Old code preserved when canceled");

  // Confirm switch via magic link
  global.window.location.search = "?household=Maple+River+42";
  handleMagicInviteLink();
  assert.strictEqual(switchModal.hidden, false, "Switch kitchen dialog shown again");
  const confirmBtn = document.querySelector("#switch-kitchen-confirm-btn");
  confirmBtn.onclick(); // Click "Switch"
  assert.strictEqual(switchModal.hidden, true, "Modal closed after switch");
  assert.strictEqual(localStorage.getItem(ACCESS_TOKEN_STORAGE), "Maple River 42", "New code applied after confirmation");

  console.log("✓ Test 17 Passed: Mom-proof onboarding, default URL collapsing, and magic invite link handling with cleanup and guard");
  passed++;

  // Test 18: Milestone 7 Part 3 — Named kitchens & wrong-kitchen guard
  // 1. Name prompt appears on fresh hosted-mode first run
  localStorage.clear();
  sessionStorage.clear();
  state.kitchenName = "";
  setHostedDefaultServerUrl("https://pantry-space.hf.space");
  localStorage.setItem(SERVER_URL_STORAGE, "https://pantry-space.hf.space");
  localStorage.setItem(ACCESS_TOKEN_STORAGE, "Maple River 42");
  setConnectionState("CONNECTED");

  const nameModal = document.querySelector("#name-onboarding-modal");
  nameModal.hidden = true;
  checkNameOnboardingPrompt();
  assert.strictEqual(nameModal.hidden, false, "Name onboarding prompt appears on fresh hosted-mode run");

  // Form "<Name>'s Kitchen" and save to server
  const nameInput = document.querySelector("#onboarding-name-input");
  nameInput.value = "Mike";
  fetchHandler = async (url, opts) => {
    if (url.includes("/api/household/name")) {
      const body = JSON.parse(opts.body);
      assert.strictEqual(body.kitchen_name, "Mike's Kitchen", "Display name formed as '<Name>'s Kitchen'");
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ kitchen_name: body.kitchen_name, created: true })
      };
    }
    return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
  };

  await submitNameOnboarding();
  assert.strictEqual(nameModal.hidden, true, "Modal hidden after name submission");
  assert.strictEqual(state.kitchenName, "Mike's Kitchen", "state.kitchenName set to Mike's Kitchen");
  assert.strictEqual(localStorage.getItem(KITCHEN_NAME_STORAGE), "Mike's Kitchen", "Kitchen name persisted in localStorage");

  // 2. Names are data, never markup: apostrophe and angle brackets
  state.kitchenName = "Mike O'Brien's Kitchen";
  renderKitchenName();
  const bannerEl = document.querySelector("#kitchen-name-banner");
  assert.strictEqual(bannerEl.textContent, "You're in: Mike O'Brien's Kitchen", "Apostrophe rendered correctly in banner");

  state.kitchenName = "<b>x</b>'s Kitchen";
  renderKitchenName();
  assert.strictEqual(bannerEl.textContent, "You're in: <b>x</b>'s Kitchen", "Angle brackets rendered as plain text in banner");
  assert.strictEqual(bannerEl.children.length, 0, "No HTML tags parsed into child DOM nodes");

  // 3. Second device with same code does not overwrite and sees existing name
  fetchHandler = async (url, opts) => {
    if (url.includes("/api/household/name")) {
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ kitchen_name: "Mike's Kitchen", created: false })
      };
    }
    return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
  };
  nameInput.value = "Alice";
  await submitNameOnboarding();
  assert.strictEqual(state.kitchenName, "Mike's Kitchen", "Second device receives and retains existing kitchen name");

  // 4. Skip for now and Settings set-later path (only while unnamed)
  localStorage.clear();
  sessionStorage.clear();
  setHostedDefaultServerUrl("https://pantry-space.hf.space");
  localStorage.setItem(SERVER_URL_STORAGE, "https://pantry-space.hf.space");
  localStorage.setItem(ACCESS_TOKEN_STORAGE, "Maple River 42");
  setConnectionState("CONNECTED");
  state.kitchenName = "";
  renderKitchenName();
  checkNameOnboardingPrompt();
  assert.strictEqual(nameModal.hidden, false, "Modal opened for unnamed kitchen");
  skipNameOnboarding();
  assert.strictEqual(nameModal.hidden, true, "Modal closed on skip");
  assert.strictEqual(sessionStorage.getItem("name_onboarding_skipped"), "true", "Skip recorded in sessionStorage");
  assert.strictEqual(state.kitchenName, "", "Kitchen remains unnamed on skip");

  const settingsNameInput = document.querySelector("#settings-kitchen-name-input");
  const settingsNameBtn = document.querySelector("#settings-kitchen-name-btn");
  renderKitchenName();
  assert.strictEqual(settingsNameInput.readOnly, false, "Settings input editable while unnamed");
  assert.strictEqual(settingsNameBtn.hidden, false, "Settings save button visible while unnamed");

  settingsNameInput.value = "Grandma";
  fetchHandler = async (url, opts) => {
    if (url.includes("/api/household/name")) {
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ kitchen_name: "Grandma's Kitchen", created: true })
      };
    }
    return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
  };
  await saveKitchenNameFromSettings();
  assert.strictEqual(state.kitchenName, "Grandma's Kitchen", "Kitchen named Grandma's Kitchen from settings");
  assert.strictEqual(settingsNameInput.readOnly, true, "Settings input now locked (readOnly)");
  assert.strictEqual(settingsNameBtn.hidden, true, "Settings save button now hidden");

  // 5. Wrong-kitchen guard on manual code change
  document.querySelector("#server-url-input").value = "https://pantry-space.hf.space";
  document.querySelector("#server-token-input").value = "Oak Ridge 99";
  fetchHandler = async () => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ ok: true, status: "ok", app: "pantry-planner" }) });
  await saveServerSettings();
  assert.strictEqual(switchModal.hidden, false, "Switch kitchen dialog triggered on manual code change");
  assert.ok(
    rawHtml.includes("This opens a different kitchen than the one you used before. Switch kitchens?"),
    "Exact switch dialog body copy in index.html"
  );
  assert.ok(rawHtml.includes(">Stay in my kitchen</button>"), "Cancel button copy matches exactly");
  assert.ok(rawHtml.includes(">Switch</button>"), "Confirm button copy matches exactly");
  cancelBtn.onclick();
  assert.strictEqual(localStorage.getItem(ACCESS_TOKEN_STORAGE), "Maple River 42", "Token untouched when switch canceled");

  document.querySelector("#server-token-input").value = "Oak Ridge 99";
  await saveServerSettings();
  confirmBtn.onclick();
  await new Promise((r) => setTimeout(r, 20));
  assert.strictEqual(localStorage.getItem(ACCESS_TOKEN_STORAGE), "Oak Ridge 99", "New code applied when switch confirmed");

  // 6. LAN / single-household mode: unchanged — no name prompt, no switch dialog
  setHostedDefaultServerUrl("");
  localStorage.setItem(SERVER_URL_STORAGE, "http://192.168.1.100:8001");
  localStorage.setItem(ACCESS_TOKEN_STORAGE, "lan-token-1");
  setConnectionState("CONNECTED");
  state.kitchenName = "";
  sessionStorage.clear();
  nameModal.hidden = true;
  switchModal.hidden = true;

  assert.strictEqual(isHostedMode(), false, "LAN server URL is not hosted mode");
  checkNameOnboardingPrompt();
  assert.strictEqual(nameModal.hidden, true, "LAN mode NEVER prompts for kitchen name");

  document.querySelector("#server-url-input").value = "http://192.168.1.100:8001";
  document.querySelector("#server-token-input").value = "lan-token-2";
  await saveServerSettings();
  assert.strictEqual(switchModal.hidden, true, "LAN mode NEVER shows switch kitchens dialog");
  assert.strictEqual(localStorage.getItem(ACCESS_TOKEN_STORAGE), "lan-token-2", "LAN token updated directly without dialog");

  console.log("✓ Test 18 Passed: Named kitchens ('<Name>'s Kitchen'), markup safety, first-set-wins, settings lock, and LAN isolation");
  passed++;

  // Test 19: Per-day servings
  setHostedDefaultServerUrl("");
  localStorage.removeItem(SERVER_URL_STORAGE);
  localStorage.removeItem(ACCESS_TOKEN_STORAGE);
  setConnectionState("DISCONNECTED");
  fetchHandler = null;
  await new Promise((r) => setTimeout(r, 50));

  // 1. Initial display at household size
  state.settings = { base_headcount: 3 };
  state.plan = {
    menu: [
      {
        day: "Monday",
        headcount: 3,
        servings: 3,
        is_overridden: false,
        breakfast: { dish_name: "Oatmeal" },
        lunch: { dish_name: "Salad" },
        dinner: { dish_name: "Pasta" },
        ingredients_to_deduct: []
      }
    ],
    shopping_list: []
  };
  renderMenu();
  const menuWrap = document.querySelector("#menu-days");
  assert.ok(menuWrap.children.length > 0, "Menu card rendered");
  let mondayCardHtml = menuWrap.children[0].innerHTML;
  assert.ok(mondayCardHtml.includes("Serves: 3"), "Day card shows Serves: 3");
  assert.ok(!mondayCardHtml.includes("· custom"), "Non-overridden day does not show custom pill");
  assert.ok(!mondayCardHtml.includes("Reset to household"), "Non-overridden day does not show reset button");
  assert.ok(mondayCardHtml.includes("servings-minus"), "Minus stepper present");
  assert.ok(mondayCardHtml.includes("servings-plus"), "Plus stepper present");

  // 2. Direct edit (Thanksgiving day 3 -> 7): custom tag, reset link, rescaling
  setConnectionState("DISCONNECTED");
  state.plan = {
    menu: [
      {
        day: "Thursday",
        headcount: 3,
        servings: 3,
        is_overridden: false,
        ingredients_to_deduct: [
          { item_name: "Chicken Broth", amount_used: 1.0, unit: "cup" },
          { item_name: "Eggs", amount_used: 2.0, unit: "egg" }
        ],
        breakfast: {}, lunch: {}, dinner: {}
      },
      {
        day: "Friday",
        headcount: 3,
        servings: 3,
        is_overridden: false,
        ingredients_to_deduct: [
          { item_name: "Rice", amount_used: 1.0, unit: "cup" }
        ],
        breakfast: {}, lunch: {}, dinner: {}
      }
    ],
    shopping_list: [
      { item_name: "Pumpkin Puree", numeric_quantity: 1.0, amount_needed: "1 can", unit: "can", reason: "For Thursday Dinner" },
      { item_name: "Turkey Breast", numeric_quantity: 1.5, amount_needed: "1.5 lbs", unit: "lbs", reason: "For Thursday Dinner" },
      { item_name: "Fresh Parsley", numeric_quantity: 1.0, amount_needed: "a pinch", unit: "pinch", reason: "For Thursday Dinner garnish" },
      { item_name: "Rolled Oats", numeric_quantity: 2.0, amount_needed: "2 cups", unit: "cups", reason: "For Monday Breakfast" }
    ]
  };
  renderMenu();

  await changeDayServings(0, 7);
  assert.strictEqual(state.plan.menu[0].servings, 7, "Thursday servings updated to 7");
  assert.strictEqual(state.plan.menu[0].is_overridden, true, "Thursday marked overridden");
  assert.strictEqual(state.plan.menu[1].servings, 3, "Friday servings untouched at 3");
  assert.strictEqual(state.plan.menu[1].is_overridden, false, "Friday not overridden");

  let thursdayCardHtml = menuWrap.children[0].innerHTML;
  assert.ok(thursdayCardHtml.includes("Serves 7 · custom"), "Overridden card displays 'Serves 7 · custom'");
  assert.ok(thursdayCardHtml.includes("Reset to household"), "Overridden card shows 'Reset to household' link");

  // Quantity rescaling verification:
  // Fractional (cup): 1.0 * (7/3) = 2.333 -> 2.25
  assert.strictEqual(state.plan.menu[0].ingredients_to_deduct[0].amount_used, 2.25, "Broth scaled 1.0 -> 2.25 cups (0.25 round)");
  // Countable (egg): 2.0 * (7/3) = 4.666 -> ceil -> 5.0
  assert.strictEqual(state.plan.menu[0].ingredients_to_deduct[1].amount_used, 5.0, "Eggs scaled 2.0 -> 5.0 eggs (ceil round)");
  // Other day untouched:
  assert.strictEqual(state.plan.menu[1].ingredients_to_deduct[0].amount_used, 1.0, "Friday rice untouched at 1.0 cup");
  // Shopping list:
  const pumpkin = state.plan.shopping_list.find(i => i.item_name === "Pumpkin Puree");
  assert.strictEqual(pumpkin.numeric_quantity, 3.0, "Pumpkin scaled 1 -> 3 cans");
  assert.ok(pumpkin.amount_needed.startsWith("3"), "Pumpkin amount_needed scaled to 3 cans");
  const turkey = state.plan.shopping_list.find(i => i.item_name === "Turkey Breast");
  assert.strictEqual(turkey.numeric_quantity, 3.5, "Turkey scaled 1.5 -> 3.5 lbs");
  assert.strictEqual(turkey.amount_needed, "3.5 lbs", "Turkey amount_needed scaled to 3.5 lbs");
  const parsley = state.plan.shopping_list.find(i => i.item_name === "Fresh Parsley");
  assert.strictEqual(parsley.amount_needed, "a pinch", "Free text quantity 'a pinch' left unchanged");
  const oats = state.plan.shopping_list.find(i => i.item_name === "Rolled Oats");
  assert.strictEqual(oats.numeric_quantity, 2.0, "Oats for Monday untouched");

  // 3. Reset to household link
  await resetDayServings(0);
  assert.strictEqual(state.plan.menu[0].servings, 3, "Thursday reset to household size (3)");
  assert.strictEqual(state.plan.menu[0].is_overridden, false, "Thursday override cleared");
  thursdayCardHtml = menuWrap.children[0].innerHTML;
  assert.ok(thursdayCardHtml.includes("Serves: 3"), "Card back to Serves: 3");
  assert.ok(!thursdayCardHtml.includes("· custom"), "Custom pill gone");
  assert.ok(!thursdayCardHtml.includes("Reset to household"), "Reset link gone");

  // 4. Mid-week household change: overrides survive, non-overridden update
  await changeDayServings(0, 7); // Thursday is overridden to 7
  applyHouseholdSizeChange(4); // Household changes 3 -> 4
  assert.strictEqual(state.plan.menu[0].servings, 7, "Overridden Thursday preserves 7");
  assert.strictEqual(state.plan.menu[0].is_overridden, true, "Thursday stays overridden");
  assert.strictEqual(state.plan.menu[1].servings, 4, "Non-overridden Friday updates to new household size 4");
  assert.strictEqual(state.plan.menu[1].is_overridden, false, "Friday remains not overridden");

  // 5. Migration of pre-feature saved plans (no servings fields)
  state.plan = {
    menu: [
      {
        day: "Saturday",
        headcount: 4,
        breakfast: {}, lunch: {}, dinner: {},
        ingredients_to_deduct: []
      }
    ],
    shopping_list: []
  };
  state.settings = { base_headcount: 4 };
  renderMenu();
  assert.strictEqual(state.plan.menu[0].servings, 4, "Pre-feature plan migrates servings to headcount 4");
  assert.strictEqual(state.plan.menu[0].is_overridden, false, "Pre-feature plan migrates is_overridden to false");
  const satCardHtml = menuWrap.children[0].innerHTML;
  assert.ok(satCardHtml.includes("Serves: 4"), "Pre-feature card renders cleanly with Serves: 4");

  console.log("✓ Test 19 Passed: Per-day servings (day card stepper, custom indicator, reset link, rounding/scaling, mid-week sync, migration)");
  passed++;

  // Test 20: Unconditional photo & receipt discard
  console.log("\nRunning Test 20: Photo & receipt discard (URL revoke, input clear, preview cleanup, zero image in storage)...");

  let revokedUrls = [];
  const origRevoke = URL.revokeObjectURL;
  URL.revokeObjectURL = (url) => {
    revokedUrls.push(url);
    if (typeof origRevoke === "function") {
      try { origRevoke(url); } catch (e) {}
    }
  };

  const mockFile = new Blob(["fake shelf photo content"], { type: "image/jpeg" });
  mockFile.name = "fridge_shelf.jpg";
  const scanInput = getOrCreateElement("scan-file");
  const scanName = getOrCreateElement("scan-file-name");
  const scanPreview = getOrCreateElement("scan-preview");
  const scanWrapper = getOrCreateElement("scan-preview-wrapper");
  const scanBtn = getOrCreateElement("scan-btn");

  scanInput.value = "C:\\fakepath\\fridge_shelf.jpg";
  applyChosenPhotos("scan", [mockFile], scanName, scanPreview, scanBtn);

  assert.ok(scanPreview.src.startsWith("blob:"), "Preview src assigned blob object URL");
  assert.strictEqual(scanPreview.hidden, false, "Preview visible after photo pick");
  assert.strictEqual(scanBtn.disabled, false, "Scan button enabled after photo pick");
  assert.strictEqual(scanName.textContent, "fridge_shelf.jpg", "Photo name displayed");

  const createdUrl = scanPreview.src;

  // Test explicit discard
  discardScanImage("scan");

  assert.ok(revokedUrls.includes(createdUrl), "Object URL was revoked via URL.revokeObjectURL");
  assert.strictEqual(scanPreview.hidden, true, "Preview hidden after discard");
  assert.strictEqual(scanPreview.src, "", "Preview src cleared after discard");
  assert.strictEqual(scanInput.value, "", "File input cleared after discard");
  assert.strictEqual(scanName.textContent, "No photo chosen yet", "File name reset to empty label");
  assert.strictEqual(scanBtn.disabled, true, "Scan button disabled after discard");
  assert.strictEqual(selectedPhotoFiles.scan, null, "selectedPhotoFiles reference wiped");

  // Test receipt discard
  const receiptFile = new Blob(["fake receipt photo content"], { type: "image/jpeg" });
  receiptFile.name = "heb_receipt.jpg";
  const receiptInput = getOrCreateElement("receipt-file");
  const receiptName = getOrCreateElement("receipt-file-name");
  const receiptPreview = getOrCreateElement("receipt-preview");
  const receiptWrapper = getOrCreateElement("receipt-preview-wrapper");
  const receiptBtn = getOrCreateElement("receipt-btn");

  receiptInput.value = "C:\\fakepath\\heb_receipt.jpg";
  applyChosenPhotos("receipt", [receiptFile], receiptName, receiptPreview, receiptBtn);
  const receiptBlobUrl = receiptPreview.src;
  assert.ok(receiptBlobUrl.startsWith("blob:"), "Receipt preview assigned blob URL");

  discardScanImage("receipt");

  assert.ok(revokedUrls.includes(receiptBlobUrl), "Receipt object URL revoked");
  assert.strictEqual(receiptPreview.hidden, true, "Receipt preview hidden");
  assert.strictEqual(receiptPreview.src, "", "Receipt preview src cleared");
  assert.strictEqual(receiptInput.value, "", "Receipt file input cleared");
  assert.strictEqual(receiptName.textContent, "No photos chosen yet", "Receipt file name reset");
  assert.strictEqual(receiptBtn.disabled, true, "Receipt scan button disabled");
  assert.strictEqual(selectedPhotoFiles.receipt, null, "Receipt selectedPhotoFiles wiped");

  // Verify localStorage contains zero image / base64 data
  for (const key of Object.keys(localStorage.store)) {
    const val = localStorage.getItem(key);
    assert.ok(!val.includes("data:image/"), `localStorage key '${key}' must not contain data URLs`);
    assert.ok(!val.includes("base64,"), `localStorage key '${key}' must not contain base64 payloads`);
  }

  console.log("✓ Test 20 Passed: Photo & receipt discard (object URL revoked, input cleared, preview cleared, localStorage clean)");
  passed++;

  // Test 21: Progress states for slow AI actions (generate and scans)
  console.log("\nRunning Test 21: Progress states for slow AI actions (generate and scans)...");

  // 1. startProgressTimer standalone verification
  const testMsgEl = { textContent: "" };
  const timer = startProgressTimer(testMsgEl, "Processing task");
  assert.strictEqual(testMsgEl.textContent, "Processing task (0s)", "Initial timer state at 0s");
  await new Promise((r) => setTimeout(r, 1100));
  assert.ok(testMsgEl.textContent.includes("(1s)"), "Timer increments elapsed time to 1s");
  timer.stop();
  const stoppedText = testMsgEl.textContent;
  await new Promise((r) => setTimeout(r, 1100));
  assert.strictEqual(testMsgEl.textContent, stoppedText, "Timer stopped successfully (no further increment)");

  // 1b. startProgressTimer 60s reassurance threshold verification
  const testReassureEl = { textContent: "" };
  const rTimer = startProgressTimer(
    testReassureEl,
    "Planning your week… this can take a few minutes.",
    "Still planning — larger households take a little longer.",
    60
  );
  assert.strictEqual(testReassureEl.textContent, "Planning your week… this can take a few minutes. (0s)", "Initial progress copy");
  rTimer.setElapsed(59);
  assert.strictEqual(testReassureEl.textContent, "Planning your week… this can take a few minutes. (59s)", "Progress copy before 60s threshold");
  rTimer.setElapsed(60);
  assert.strictEqual(testReassureEl.textContent, "Still planning — larger households take a little longer. (60s)", "Reassurance copy at 60s threshold");
  rTimer.setElapsed(61);
  assert.strictEqual(testReassureEl.textContent, "Still planning — larger households take a little longer. (61s)", "Reassurance copy keeps counting after 60s");
  rTimer.stop();

  // Setup connection state for API actions
  setConnectionState("CONNECTED");
  localStorage.setItem(KEY_STORAGE, "mock-key");

  // 2. generatePlan() in-flight progress, button disabled, resolve & reject
  const generateBtn = getOrCreateElement("generate-btn");
  const menuMsg = getOrCreateElement("menu-msg");
  generateBtn.disabled = false;

  let resolveGenerate;
  fetchHandler = (url, opts) => {
    if (url.includes("/api/plan/generate")) {
      return new Promise((resolve) => {
        resolveGenerate = () => resolve({
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => ({
            plan_id: 10,
            created_at: new Date().toISOString(),
            plan: { menu: [], shopping_list: [] }
          })
        });
      });
    }
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) });
  };

  const genP = generatePlan();
  // While in flight:
  assert.strictEqual(generateBtn.disabled, true, "Generate button disabled while generating");
  assert.strictEqual(menuMsg.textContent, "Planning your week… this can take a few minutes. (0s)", "Generate progress message initialized");

  // Resolve generate
  resolveGenerate();
  await genP;
  assert.strictEqual(generateBtn.disabled, false, "Generate button re-enabled on resolve");
  assert.strictEqual(menuMsg.textContent, "✅ Weekly Menu & Recipes ready!", "Generate success message shown");

  // Generate on reject / failure
  fetchHandler = (url, opts) => {
    if (url.includes("/api/plan/generate")) {
      return Promise.resolve({
        ok: false,
        status: 500,
        headers: { get: () => "application/json" },
        json: async () => ({ detail: "AI generation failed: timeout" })
      });
    }
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) });
  };

  await generatePlan();
  assert.strictEqual(generateBtn.disabled, false, "Generate button re-enabled on reject");
  assert.ok(menuMsg.textContent.includes("AI generation failed"), "Error message displayed on reject");

  // 3. scanShelfPhoto() in-flight progress, button disabled, resolve & reject
  const scanBtnEl = getOrCreateElement("scan-btn");
  const scanMsgEl = getOrCreateElement("scan-msg");
  const scanInputEl = getOrCreateElement("scan-file");
  const scanNameEl = getOrCreateElement("scan-file-name");
  const scanPreviewEl = getOrCreateElement("scan-preview");

  const shelfFile = new Blob(["shelf photo"], { type: "image/jpeg" });
  shelfFile.name = "shelf.jpg";
  applyChosenPhotos("scan", [shelfFile], scanNameEl, scanPreviewEl, scanBtnEl);

  let resolveScan;
  fetchHandler = (url, opts) => {
    if (url.includes("/api/scan-photo")) {
      return new Promise((resolve) => {
        resolveScan = () => resolve({
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => ({
            inventory: [],
            added: 3,
            served_by: "Gemini"
          })
        });
      });
    }
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) });
  };

  const scanP = scanShelfPhoto();
  assert.strictEqual(scanBtnEl.disabled, true, "Scan button disabled while scanning");
  assert.strictEqual(scanMsgEl.textContent, "Reading your photo… (0s)", "Photo scan progress message initialized");

  resolveScan();
  await scanP;
  assert.ok(scanMsgEl.textContent.includes("Added 3 items"), "Scan success message shown");
  assert.strictEqual(scanBtnEl.disabled, true, "Scan button disabled after discard");

  // Scan shelf photo on reject
  applyChosenPhotos("scan", [shelfFile], scanNameEl, scanPreviewEl, scanBtnEl);
  fetchHandler = (url, opts) => {
    if (url.includes("/api/scan-photo")) {
      return Promise.resolve({
        ok: false,
        status: 500,
        headers: { get: () => "application/json" },
        json: async () => ({ detail: "Vision scan error" })
      });
    }
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) });
  };

  await scanShelfPhoto();
  assert.ok(scanMsgEl.textContent.includes("Vision scan error"), "Scan error message shown on reject");
  assert.strictEqual(scanBtnEl.disabled, true, "Scan button disabled after discard on error");

  // 4. scanReceipt() in-flight progress, button disabled, resolve & reject
  const receiptBtnEl = getOrCreateElement("receipt-btn");
  const receiptMsgEl = getOrCreateElement("receipt-msg");
  const receiptNameEl = getOrCreateElement("receipt-file-name");
  const receiptPreviewEl = getOrCreateElement("receipt-preview");
  const receiptFileEl = new Blob(["receipt photo"], { type: "image/jpeg" });
  receiptFileEl.name = "receipt.jpg";
  applyChosenPhotos("receipt", [receiptFileEl], receiptNameEl, receiptPreviewEl, receiptBtnEl);

  let resolveReceipt;
  fetchHandler = (url, opts) => {
    if (url.includes("/api/receipt/scan")) {
      return new Promise((resolve) => {
        resolveReceipt = () => resolve({
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => ({
            inventory: [],
            plan: null,
            logs: ["Milk $3.99"],
            store_name: "HEB",
            served_by: "Gemini"
          })
        });
      });
    }
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) });
  };

  const receiptP = scanReceipt();
  assert.strictEqual(receiptBtnEl.disabled, true, "Receipt button disabled while scanning");
  assert.strictEqual(receiptMsgEl.textContent, "Reading your receipt… (0s)", "Receipt progress message initialized");

  resolveReceipt();
  await receiptP;
  assert.ok(receiptMsgEl.textContent.includes("Restocked 1 items"), "Receipt success message shown");
  assert.strictEqual(receiptBtnEl.disabled, true, "Receipt button disabled after discard");

  // Receipt on reject
  applyChosenPhotos("receipt", [receiptFileEl], receiptNameEl, receiptPreviewEl, receiptBtnEl);
  fetchHandler = (url, opts) => {
    if (url.includes("/api/receipt/scan")) {
      return Promise.resolve({
        ok: false,
        status: 500,
        headers: { get: () => "application/json" },
        json: async () => ({ detail: "Receipt parsing error" })
      });
    }
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) });
  };

  await scanReceipt();
  assert.ok(receiptMsgEl.textContent.includes("Receipt parsing error"), "Receipt error message shown on reject");
  assert.strictEqual(receiptBtnEl.disabled, true, "Receipt button disabled after discard on error");

  console.log("✓ Test 21 Passed: Progress states for slow AI actions (generate and scans, elapsed counting, resolve & reject)");
  passed++;

  // Test 22: Server setup reachability and key-prompt trap fix (Requirements R1, R2, R3)
  console.log("\nRunning Test 22: Server setup reachability and key-prompt trap fix...");

  // Initialize event listeners
  init();

  const setupScreenEl = document.querySelector("#setup-screen");
  const serverSetupScreenEl = document.querySelector("#server-setup-screen");
  const setupToServerBtnEl = document.querySelector("#setup-to-server-btn");
  const unreachableModalEl = document.querySelector("#server-unreachable-modal");
  const unreachableTextEl = document.querySelector("#server-unreachable-text");
  const unreachableEditBtnEl = document.querySelector("#server-unreachable-edit-btn");
  const unreachableRetryBtnEl = document.querySelector("#server-unreachable-retry-btn");
  const unreachableCloseBtnEl = document.querySelector("#server-unreachable-close-btn");
  const setupServerCloseBtnEl = document.querySelector("#setup-server-close-btn");
  const headerServerBtnEl = document.querySelector("#header-server-btn");
  const bannerSettingsBtnEl = document.querySelector("#banner-settings-btn");

  // Verify HTML markup has escape button and unreachable modal
  const staticHtml = fs.readFileSync(indexHtmlPath, "utf-8");
  assert.ok(staticHtml.includes('id="setup-to-server-btn"'), "setup-to-server-btn exists in HTML markup");
  assert.ok(staticHtml.includes('id="server-unreachable-modal"'), "server-unreachable-modal exists in HTML markup");
  assert.ok(staticHtml.includes('id="header-server-btn"'), "header-server-btn exists in HTML markup");
  assert.ok(staticHtml.includes('id="setup-server-close-btn"'), "setup-server-close-btn exists in HTML markup");

  // 1. (a) Key-prompt screen exposes the server-setup escape and it opens the connection page
  localStorage.clear();
  sessionStorage.clear();
  setHostedDefaultServerUrl("");
  setConnectionState("DISCONNECTED");

  showSetup();
  assert.strictEqual(setupScreenEl.hidden, false, "Key prompt screen is visible");
  assert.strictEqual(serverSetupScreenEl.hidden, true, "Server setup is hidden initially");

  // Click the escape button from key prompt screen
  setupToServerBtnEl.click();
  assert.strictEqual(setupScreenEl.hidden, true, "Key prompt screen is hidden after clicking server escape");
  assert.strictEqual(serverSetupScreenEl.hidden, false, "Server connection page is opened after clicking server escape");
  hideServerSetup();
  assert.strictEqual(serverSetupScreenEl.hidden, true, "Server setup hidden after close");

  // 2. (b) With server URL stored, no AI action renders a key prompt even when server is unreachable;
  //        the server-unreachable state with edit-address button renders instead.
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem(SERVER_URL_STORAGE, "http://192.168.4.162:8001");
  setConnectionState("DISCONNECTED");

  assert.strictEqual(hasServerConfigured(), true, "hasServerConfigured returns true when server URL is stored");
  assert.strictEqual(isOnline(), false, "isOnline returns false when disconnected");

  // Calling requireKey while disconnected with server stored must NOT show setup-screen
  setupScreenEl.hidden = true;
  unreachableModalEl.hidden = true;
  const canProceed = requireKey();
  assert.strictEqual(canProceed, false, "requireKey returns false when offline");
  assert.strictEqual(setupScreenEl.hidden, true, "Key prompt screen is NEVER shown when server URL is configured");
  assert.strictEqual(unreachableModalEl.hidden, false, "Server unreachable modal is shown instead");
  assert.ok(unreachableTextEl.textContent.includes("192.168.4.162:8001"), "Unreachable modal contains stored server address");
  assert.ok(unreachableTextEl.textContent.includes("can't be reached"), "Unreachable modal contains plain-language explanation");

  // Clicking "Edit Server Address" button from unreachable modal opens server setup
  unreachableEditBtnEl.click();
  assert.strictEqual(unreachableModalEl.hidden, true, "Unreachable modal hidden on edit click");
  assert.strictEqual(serverSetupScreenEl.hidden, false, "Server setup screen opened from unreachable modal");
  const setupUrlInput = document.querySelector("#setup-server-url");
  assert.strictEqual(setupUrlInput.value, "http://192.168.4.162:8001", "Server setup input pre-populated with stored address");
  hideServerSetup();

  // Test generatePlan() while unreachable with server stored
  setupScreenEl.hidden = true;
  unreachableModalEl.hidden = true;
  await generatePlan();
  assert.strictEqual(setupScreenEl.hidden, true, "generatePlan NEVER shows key prompt when server configured");
  assert.strictEqual(unreachableModalEl.hidden, false, "generatePlan displays unreachable modal when disconnected");
  hideServerUnreachableModal();

  // Test scanShelfPhoto() while unreachable with server stored
  const mockShelfFile = new Blob(["shelf photo"], { type: "image/jpeg" });
  mockShelfFile.name = "shelf.jpg";
  applyChosenPhotos("scan", [mockShelfFile], document.querySelector("#scan-file-name"), document.querySelector("#scan-preview"), document.querySelector("#scan-btn"));
  setupScreenEl.hidden = true;
  unreachableModalEl.hidden = true;
  await scanShelfPhoto();
  assert.strictEqual(setupScreenEl.hidden, true, "scanShelfPhoto NEVER shows key prompt when server configured");
  assert.strictEqual(unreachableModalEl.hidden, false, "scanShelfPhoto displays unreachable modal when disconnected");
  hideServerUnreachableModal();

  // Test scanReceipt() while unreachable with server stored
  const mockReceiptFile = new Blob(["receipt photo"], { type: "image/jpeg" });
  mockReceiptFile.name = "receipt.jpg";
  applyChosenPhotos("receipt", [mockReceiptFile], document.querySelector("#receipt-file-name"), document.querySelector("#receipt-preview"), document.querySelector("#receipt-btn"));
  setupScreenEl.hidden = true;
  unreachableModalEl.hidden = true;
  await scanReceipt();
  assert.strictEqual(setupScreenEl.hidden, true, "scanReceipt NEVER shows key prompt when server configured");
  assert.strictEqual(unreachableModalEl.hidden, false, "scanReceipt displays unreachable modal when disconnected");
  hideServerUnreachableModal();

  // 3. (c) From the connected main UI, server setup can be opened and stored address changed
  setConnectionState("CONNECTED");
  updateConnectionUI();

  const headerDotEl = document.querySelector("#header-server-dot");
  const headerLabelEl = document.querySelector("#header-server-label");
  assert.strictEqual(headerDotEl.classList.contains("connected"), true, "Header server dot shows connected state");
  assert.strictEqual(headerLabelEl.textContent, "Connected", "Header server label shows 'Connected'");

  // Open server setup via header button
  serverSetupScreenEl.hidden = true;
  headerServerBtnEl.click();
  assert.strictEqual(serverSetupScreenEl.hidden, false, "Server setup opened from header button in main UI");

  // Cancel / close button closes modal without saving
  setupServerCloseBtnEl.click();
  assert.strictEqual(serverSetupScreenEl.hidden, true, "Cancel button closes server setup screen");

  // Re-open and change address to new hub IP
  headerServerBtnEl.click();
  assert.strictEqual(serverSetupScreenEl.hidden, false, "Server setup re-opened from header");
  setupUrlInput.value = "http://192.168.1.9:8001";

  fetchHandler = async (url) => {
    if (url.includes("192.168.1.9:8001/health")) {
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ ok: true, status: "ok", app: "pantry-planner" })
      };
    }
    return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
  };

  const setupTestBtn = document.querySelector("#setup-server-test-btn");
  await setupTestBtn.onclick();
  await new Promise((r) => setTimeout(r, 700));

  assert.strictEqual(storedServerUrl(), "http://192.168.1.9:8001", "Stored server URL updated to new IP");
  assert.strictEqual(localStorage.getItem(SERVER_URL_STORAGE), "http://192.168.1.9:8001", "New server URL persisted in localStorage");
  assert.strictEqual(serverSetupScreenEl.hidden, true, "Server setup closed after successful connect");

  // Also verify banner Settings button opens server setup when disconnected
  setConnectionState("DISCONNECTED");
  updateConnectionUI();
  serverSetupScreenEl.hidden = true;
  bannerSettingsBtnEl.click();
  assert.strictEqual(serverSetupScreenEl.hidden, false, "Server setup opened from connection banner Settings button");
  hideServerSetup();

  // 4. (d) Regression: Standalone mode with NO server stored still offers original key flow unchanged
  localStorage.clear();
  sessionStorage.clear();
  setHostedDefaultServerUrl("");
  setConnectionState("DISCONNECTED");

  assert.strictEqual(hasServerConfigured(), false, "hasServerConfigured is false in standalone mode");
  setupScreenEl.hidden = true;
  const standaloneCheck = requireKey();
  assert.strictEqual(standaloneCheck, false, "requireKey returns false when standalone has no key");
  assert.strictEqual(setupScreenEl.hidden, false, "Original key prompt shown when in standalone mode with no keys");
  hideSetup();

  console.log("✓ Test 22 Passed: Server setup reachability and key-prompt trap fix (escape door, unreachable modal, header access, address update, standalone regression)");
  passed++;

  // ==========================================
  // Test 23: Honest errors and connection state hygiene (Part 2)
  // - simulated scan 503-with-detail → server message displayed, connection state stays CONNECTED, no "offline" wording
  // - simulated probe transport failure → DISCONNECTED + banner
  // - successful refresh after a failure → error text cleared
  // ==========================================
  {
    console.log("\nRunning Test 23: Honest errors and connection state hygiene...");
    localStorage.clear();
    localStorage.setItem(SERVER_URL_STORAGE, "http://192.168.1.9:8001");
    setConnectionState("CONNECTED");
    updateConnectionUI();

    // 1. Simulated scan 503-with-detail
    const scanMsgEl = document.querySelector("#scan-msg");
    selectedPhotoFiles.scan = [{ name: "test.jpg", size: 1000, type: "image/jpeg" }];

    fetchHandler = async (url, opts) => {
      if (url.includes("/api/scan-photo")) {
        return {
          ok: false,
          status: 503,
          headers: { get: () => "application/json" },
          json: async () => ({
            detail: "All AI services are unavailable right now — Gemini, Groq, and OpenRouter were all tried. Please try again in a few minutes."
          }),
        };
      }
      return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
    };

    await scanShelfPhoto();

    assert.strictEqual(
      connectionState,
      "CONNECTED",
      "simulated scan 503-with-detail: connection state must stay CONNECTED (server was reached)"
    );
    assert.ok(
      scanMsgEl.textContent.includes("All AI services are unavailable right now — Gemini, Groq, and OpenRouter were all tried"),
      "simulated scan 503-with-detail: server message displayed in status area"
    );
    assert.strictEqual(
      scanMsgEl.textContent.toLowerCase().includes("offline"),
      false,
      "simulated scan 503-with-detail: no 'offline' wording when server answers with 503"
    );
    assert.strictEqual(
      scanMsgEl.textContent.toLowerCase().includes("cannot reach server"),
      false,
      "simulated scan 503-with-detail: no 'Cannot reach server' wording"
    );

    // 2. Simulated probe transport failure
    fetchHandler = async (url) => {
      if (url.includes("/health")) {
        throw new Error("Failed to fetch");
      }
      return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
    };

    await checkHeartbeat();

    assert.strictEqual(
      connectionState,
      "DISCONNECTED",
      "simulated probe transport failure: probe transport failure sets connectionState to DISCONNECTED"
    );
    const bannerEl = document.querySelector("#connection-banner");
    assert.strictEqual(
      bannerEl.hidden,
      false,
      "simulated probe transport failure: disconnected banner is displayed"
    );
    assert.ok(
      document.querySelector("#banner-text").textContent.includes("Disconnected from"),
      "simulated probe transport failure: banner text shows disconnected"
    );

    // 3. Successful refresh after a failure -> error text cleared
    fetchHandler = async (url) => {
      if (url.includes("/health")) {
        return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ ok: true, status: "ok", app: "pantry-planner" }) };
      }
      if (url.includes("/api/inventory")) return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => [] };
      if (url.includes("/api/settings")) return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ base_headcount: 2 }) };
      if (url.includes("/api/plan/latest")) return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ plan_id: null, plan: null }) };
      return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
    };

    await refreshAll();

    assert.strictEqual(connectionState, "CONNECTED", "successful refresh restores connectionState to CONNECTED");
    assert.strictEqual(scanMsgEl.textContent, "", "successful refresh after a failure: scan error text cleared");
    assert.strictEqual(document.querySelector("#menu-msg").textContent, "", "successful refresh after a failure: menu error text cleared");

    console.log("✓ Test 23 Passed: Honest errors and connection state hygiene (scan 503 detail, probe transport failure, error text cleared)");
    passed++;
  }

  // ==========================================
  // Test 24: Offline destructive-op enforcement (Part 3)
  // - offline save/clear attempt issues zero mutating requests
  // ==========================================
  {
    console.log("\nRunning Test 24: Offline destructive-op enforcement...");
    setConnectionState("DISCONNECTED");
    updateConnectionUI();

    const invSaveBtnEl = document.querySelector("#inv-save-btn");
    const invClearBtnEl = document.querySelector("#inv-clear-btn");

    assert.strictEqual(invSaveBtnEl.disabled, true, "inv-save-btn is disabled while DISCONNECTED");
    assert.strictEqual(invClearBtnEl.disabled, true, "inv-clear-btn is disabled while DISCONNECTED");

    let mutatingCalls = 0;
    fetchHandler = async (url, opts) => {
      const method = (opts && opts.method) || "GET";
      if (url.includes("/api/inventory") && (method === "PUT" || method === "DELETE")) {
        mutatingCalls++;
      }
      return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
    };

    // Attempt saveInventory()
    await saveInventory();
    assert.strictEqual(mutatingCalls, 0, "offline saveInventory issues zero mutating requests");

    // Attempt direct api() calls for PUT and DELETE
    let putBlocked = false;
    try {
      await api("/api/inventory", { method: "PUT", body: [] });
    } catch (e) {
      putBlocked = true;
      assert.ok(e.message.includes("disconnected"), "PUT /api/inventory throws error when disconnected");
    }
    assert.strictEqual(putBlocked, true, "PUT /api/inventory was blocked at api() layer");

    let deleteBlocked = false;
    try {
      await api("/api/inventory?confirm_clear=true", { method: "DELETE" });
    } catch (e) {
      deleteBlocked = true;
      assert.ok(e.message.includes("disconnected"), "DELETE /api/inventory throws error when disconnected");
    }
    assert.strictEqual(deleteBlocked, true, "DELETE /api/inventory was blocked at api() layer");
    assert.strictEqual(mutatingCalls, 0, "offline save/clear attempt issues zero mutating requests");

    // Hand-adds continue to queue via existing outbox
    localStorage.removeItem(OUTBOX_STORAGE);
    queueOfflineAdd({
      item_name: "Hand Item 1",
      quantity: 2,
      unit: "can",
      location: "Cupboard / Pantry",
      category: "Pantry/Dry"
    });
    const queuedOutbox = getOutbox();
    assert.strictEqual(queuedOutbox.length, 1, "Hand-adds continue to queue via existing outbox while offline");
    assert.strictEqual(queuedOutbox[0].item_name, "Hand Item 1", "Queued item has expected name");

    console.log("✓ Test 24 Passed: Offline destructive-op enforcement (control disabled, api layer blocked, outbox queued)");
    passed++;
  }

  // ==========================================
  // Test 25: AI Services Status Panel & Model Config (Provider Resilience)
  // - desktop format shows model, status_word, checked time
  // - connected phone format sanitizes models, shows plain status word
  // - model save validation rejects bogus with exact copy, saves valid
  // - probe button triggers on-demand check
  // ==========================================
  {
    console.log("\nRunning Test 25: AI Services Status Panel & Model Config (Resilience Add-on)...");
    localStorage.clear();
    setConnectionState("CONNECTED");

    // 1. Desktop mode status rendering
    const mockStatusDesktop = {
      providers: {
        gemini: {
          name: "Gemini",
          model: "gemini-3.8-flash",
          state: "ok",
          status_word: "Working",
          checked_display: "9:41 AM",
        },
        groq: {
          name: "Groq (scans)",
          model: "qwen/qwen3.8-27b",
          state: "ok",
          status_word: "Working",
          checked_display: "9:41 AM",
        },
        groq_generate: {
          name: "Groq (plans)",
          model: "openai/gpt-oss-120b",
          state: "ok",
          status_word: "Working",
          checked_display: "9:41 AM",
        },
        openrouter: {
          name: "OpenRouter",
          model: "nvidia/nemotron-3.5-super-vl:free",
          state: "down",
          status_word: "Down",
          checked_display: "9:41 AM",
        },
      },
    };

    renderProviderStatus(mockStatusDesktop);

    const groqSummaryEl = document.querySelector("#status-row-groq .provider-summary-text");
    assert.ok(groqSummaryEl, "Groq summary element exists");
    assert.strictEqual(
      groqSummaryEl.textContent,
      "Groq (scans) — qwen/qwen3.8-27b — Working — checked 9:41 AM",
      "Desktop status line matches exact required format: Groq (scans) — qwen/qwen3.8-27b — Working — checked 9:41 AM"
    );

    const groqGenSummaryEl = document.querySelector("#status-row-groq_generate .provider-summary-text");
    assert.ok(groqGenSummaryEl, "Groq generate summary element exists");
    assert.strictEqual(
      groqGenSummaryEl.textContent,
      "Groq (plans) — openai/gpt-oss-120b — Working — checked 9:41 AM",
      "Desktop status line matches exact required format: Groq (plans) — openai/gpt-oss-120b — Working — checked 9:41 AM"
    );

    const geminiSummaryEl = document.querySelector("#status-row-gemini .provider-summary-text");
    assert.strictEqual(
      geminiSummaryEl.textContent,
      "Gemini — gemini-3.8-flash — Working — checked 9:41 AM",
      "Gemini status line matches expected format"
    );

    // 2. Phone client mode (storedServerUrl present) -> sanitized display
    localStorage.setItem(SERVER_URL_STORAGE, "http://192.168.1.9:8001");
    const mockStatusPhone = {
      providers: {
        gemini: { name: "Gemini", state: "ok", status_word: "Working" },
        groq: { name: "Groq (scans)", state: "ok", status_word: "Working" },
        groq_generate: { name: "Groq (plans)", state: "ok", status_word: "Working" },
        openrouter: { name: "OpenRouter", state: "down", status_word: "Down" },
      },
    };
    renderProviderStatus(mockStatusPhone);

    assert.strictEqual(
      groqSummaryEl.textContent,
      "Groq (scans) — Working",
      "Phone client receives sanitized status word only (no model or internal error)"
    );
    assert.strictEqual(
      groqGenSummaryEl.textContent,
      "Groq (plans) — Working",
      "Phone client receives sanitized status word for Groq plans"
    );
    const orSummaryEl = document.querySelector("#status-row-openrouter .provider-summary-text");
    assert.strictEqual(
      orSummaryEl.textContent,
      "OpenRouter — Down",
      "Phone client receives sanitized Down status"
    );

    // 3. Test saveProviderModel validation failure keeps original and displays exact error message
    localStorage.removeItem(SERVER_URL_STORAGE); // return to desktop mode
    const groqInput = document.querySelector("#groq-model-input");
    const groqMsg = document.querySelector("#groq-model-msg");
    groqInput.value = "bogus-model-123";
    groqInput.dataset.originalModel = "qwen/qwen3.8-27b";

    fetchHandler = async (url, opts) => {
      if (url.includes("/api/providers/model")) {
        return {
          ok: false,
          status: 400,
          headers: { get: () => "application/json" },
          json: async () => ({
            ok: false,
            error: "That model isn't available from Groq right now — check the name and try again.",
          }),
        };
      }
      return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
    };

    await saveProviderModel("groq");
    assert.strictEqual(
      groqMsg.textContent,
      "That model isn't available from Groq right now — check the name and try again.",
      "Exact plain error displayed when model validation probe fails"
    );
    assert.strictEqual(groqInput.value, "qwen/qwen3.8-27b", "Reverts input to original model on validation failure");

    // 4. Test saveProviderModel success updates input and originalModel
    groqInput.value = "meta-llama/llama-4-scout-17b-16e-instruct";
    fetchHandler = async (url, opts) => {
      if (url.includes("/api/providers/model")) {
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => ({
            ok: true,
            provider: "groq",
            model: "meta-llama/llama-4-scout-17b-16e-instruct",
            message: "Groq model updated to meta-llama/llama-4-scout-17b-16e-instruct.",
          }),
        };
      }
      if (url.includes("/api/providers/status")) {
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => mockStatusDesktop,
        };
      }
      return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
    };

    await saveProviderModel("groq");
    assert.ok(groqMsg.textContent.includes("Groq model updated"), "Success message displayed on valid model save");
    assert.strictEqual(groqInput.dataset.originalModel, "qwen/qwen3.8-27b");

    // 5. Test saveProviderModel for groq_generate (plans slot)
    const groqGenInput = document.querySelector("#groq_generate-model-input");
    const groqGenMsg = document.querySelector("#groq_generate-model-msg");
    if (groqGenInput && groqGenMsg) {
      groqGenInput.value = "openai/gpt-oss-120b";
      fetchHandler = async (url, opts) => {
        if (url.includes("/api/providers/model")) {
          return {
            ok: true,
            status: 200,
            headers: { get: () => "application/json" },
            json: async () => ({
              ok: true,
              provider: "groq_generate",
              model: "openai/gpt-oss-120b",
              message: "Groq model updated to openai/gpt-oss-120b.",
            }),
          };
        }
        return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({}) };
      };
      await saveProviderModel("groq_generate");
      assert.ok(groqGenMsg.textContent.includes("Groq model updated"), "Success message displayed on valid groq_generate save");
      assert.strictEqual(groqGenInput.dataset.originalModel, "openai/gpt-oss-120b");
    }

    console.log("✓ Test 25 Passed: AI Services Status Panel & Model Config (desktop formatting, phone sanitization, model validation probe)");
    passed++;
  }

  console.log(`\nAll ${passed} Mobile Client tests passed successfully!`);
}


runTests().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});



