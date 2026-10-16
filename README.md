# Pantry Planner — Desktop Edition

A desktop-first version of the Pantry-to-Menu Planner: snap (or drag & drop)
photos of your fridge, freezer, and cupboard; get a dietary-aware 7-day menu
with step-by-step recipes, automatic inventory deduction when you cook, a
categorized shopping list, and one-photo receipt restocking.

A FastAPI backend (`main.py`) serves the REST API and a wide-screen web UI
(`static/`) designed for a big monitor: left sidebar navigation, photo panel
beside the inventory table, a 7-day week grid, and the shopping list and
receipt scanner side by side.

> **Sibling project:** a mobile version (API + installable phone web app)
> lives in `~/workspace/projects/pantry-planner/api/`. When you turn on **phone
> access** (see below) the phone app uses **this** app's database, so desktop and
> phone share one pantry. The mobile project's own backend still works on its own
> with its own `pantry_planner.db`, but the two don't mix unless the phone points here.

## Install

```bash
cd ~/workspace/projects/pantry-planner-desktop
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

(`pywebview` is in the requirements for the native-window launcher; if it
fails to install on your system, the launcher simply falls back to your
default browser — see below.)

## Run it

Native desktop window (recommended):

```bash
.venv/bin/python run_desktop.py
```

This starts the server on port **8001** in the background and opens the app
in a "Pantry Planner" window (1280×860). If `pywebview` isn't available, it
opens http://127.0.0.1:8001 in your default browser instead and keeps
serving until Ctrl+C.

Or run the server only and open it yourself:

```bash
.venv/bin/uvicorn main:app --port 8001
# → http://127.0.0.1:8001
```

## Gemini key (bring your own — it's free)

The app uses Google's free Gemini AI tier, and every user brings their own
key. The first time you open the app, a setup screen walks you through it:

1. Click **🔑 Get My Free Gemini Key** (opens https://aistudio.google.com/apikey — no credit card).
2. Paste the key into the app and click **Save & Connect**.
3. The app checks the key against Google (one tiny test call via
   `POST /api/validate-key`) and only saves it on this computer (browser
   localStorage) after Google accepts it.

You can replace the key anytime in the **Household & Diet** view
(🔑 Gemini API Key section). The key is stored only on your device, is only
ever sent to Google through the backend, and is never logged.
Alternatively, start the server with the `GEMINI_API_KEY` environment
variable set and no per-device key is needed.

## Desktop extras

- **Drag & drop** shelf and receipt photos onto the scan panels (clicking
  the panel opens a file picker instead).
- **Wide week grid** — all seven day cards side by side, recipes expandable.
- **Inventory as a table** — edit quantities, units, locations, and
  categories in place; saving preserves row IDs so cooked-meal deductions
  keep matching the right items.
- **Print the shopping list** — the 🖨️ Print button prints just the list
  (with the week's menu summary), not the whole app.

## API endpoints

Identical to the mobile backend:

| Endpoint | What it does |
|---|---|
| `GET /health` | Liveness check |
| `GET /api/inventory` | List inventory |
| `POST /api/inventory` | Add items `{location, items[]}` |
| `PUT /api/inventory` | Bulk-replace inventory (existing row IDs are preserved) |
| `DELETE /api/inventory` | Clear all inventory |
| `GET /api/settings` / `PUT /api/settings` | Household & diet settings |
| `POST /api/scan-photo` | Multipart photo + `location` → Gemini shelf scan |
| `POST /api/plan/generate` | `{daily_headcounts, theme_name}` → new 7-day plan |
| `GET /api/plan/latest` | Latest saved plan |
| `POST /api/plan/cook` | `{day_index}` → deduct that day's ingredients |
| `POST /api/shopping/restock` | `{indices}` → add checked items to inventory |
| `POST /api/shopping/clear-purchased` | Remove all purchased items from the latest plan's shopping list |
| `POST /api/receipt/scan` | Multipart receipt photo → auto-restock + check off list |
| `GET /api/shopping-list/export?include_purchased=bool` | Shopping list as Markdown |
| `POST /api/validate-key` | Validate the `X-Gemini-Key` header with one tiny Gemini call |
| `GET /` | The desktop web UI |

Gemini-backed endpoints accept the key via the `X-Gemini-Key` header
(the web UI sends it automatically) or fall back to `GEMINI_API_KEY`.

## Phone access (shared database with the phone app)

This desktop app is the **hub**: its `pantry_planner.db` is the one shared copy.
The phone app reads and writes it through this server over your home Wi-Fi — no
separate sync system. Enter an item once, it shows up on both.

- **Off by default.** On first run the app asks once whether to enable phone
  access and remembers your answer (`pantry_desktop_config.json`, git-ignored).
  Change it any time under **Connect Phone** in the sidebar. Turning it on makes
  the server listen on your home network (`0.0.0.0`) instead of only this PC
  (`127.0.0.1`); the app restarts its server for you when you flip the switch.
- **Connect Phone page** shows the address to type into the phone app (e.g.
  `http://192.168.4.159:8001`) and a QR code for it.
- **Access code (optional, off by default).** If set, the phone must send it
  (`X-Pantry-Token`) for every `/api/*` call; this PC never needs it. It is a
  light guard for a home network, **not strong security** — traffic is plain HTTP.
- With phone access **off**, any request from another device is refused with a
  clear message, even if the server were started by hand on the network.
- **Windows Firewall:** the first time the server listens on the network, Windows
  may ask to allow Python. Allow it for your home network.
- **Tip:** give this PC a fixed IP (DHCP reservation) in your router so the
  address doesn't change. A router's guest-network / "client isolation" mode can
  block phone-to-PC traffic entirely.
- The phone's Gemini key stays on the phone and is sent per request, exactly as
  before. `GEMINI_API_KEY` set on this PC acts as a fallback for any client that
  sends no key.

`/api/desktop/*` (phone-access settings, QR code) only answers this PC itself.

### Tests

```bash
py -m unittest discover -s tests -v
```

Uses temporary database/config files (`PANTRY_DB_FILE`, `PANTRY_CONFIG_FILE`);
your real data is not touched.

## Notes

- The database is `pantry_planner.db` in this project's root. Set
  `PANTRY_DB_FILE` to point the backend at a different SQLite file
  (handy for testing).
