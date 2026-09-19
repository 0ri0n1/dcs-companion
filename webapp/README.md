# DCS Companion dashboard

The local browser application for [DCS Companion](../README.md), built with FastAPI and React/TypeScript. Start with the root [requirements and quickstart](../README.md#first-run) and the [installation guide](docs/INSTALL.md).

The default address is [http://127.0.0.1:18787](http://127.0.0.1:18787). The launcher starts or reuses the existing collector. Preview/dry-run is the default; viewing data, selecting scope items and pairing a phone do not send cockpit inputs.

## Using the app

**Navigation Scope** combines local airfield/navaid reference with fresh ownship data. Friendlies and Enemies use the same layer controls as the other scope layers. Select a map marker or Mission Awareness row to update the existing **DIRECT TO · DISPLAY ONLY** strip below the scope. Aircraft details show distance, bearing, heading, ground speed and altitude MSL, with ground track when available. On a phone, selection reveals that strip above the bottom navigation. Mission Awareness remains a separate list/status panel.

Mission contacts require the compatible optional local bridge and verified single-player context. They represent coalition membership, not aircraft sensor detection. Stale or mismatched contacts disappear. Aircraft arrows use verified ground track or heading; missing direction stays unknown.

**Remote → All displays** shows the Hornet's Left DDI, AMPCD and Right DDI with their cockpit bezel controls. Phones stack the same displays and controls. Live images require explicit DCS export/capture setup; exported text remains a separate source. Other aircraft can expose generic text displays without inheriting Hornet controls or video support.

On a paired phone, use **Connect controls → Enable live controls**, then keep DCS focused on the PC. Buttons with missing or conflicting bindings remain unavailable. Disconnect applies to that screen; Stop cancels shared remote work. Workflow editing is PC-only. Reloads and reconnects never replay commands. See [Remote](docs/REMOTE.md).

**Controls** shows supported aircraft assignments, conflicts and favourites. **Cockpit**, **Sensors**, **Flight data** and **Data Health** expose exported information, its freshness and connection diagnostics. **Your setup** selects the local DCS installation/profile; **Appearance** stores display preferences on that browser.

For phone access, use **Start Tablet Dashboard.cmd** and **Pair phone** on the PC. It uses trusted-LAN HTTP, requires separate firewall permission where needed, and does not turn on live controls. Backend restarts invalidate phone pairing. See [Connections](docs/CONNECTIONS.md).

## Build and run

Run **Setup Dashboard.cmd** from this folder for the standard dependency installation and production build. It does not install the exporter, mission bridge or display driver.

Equivalent commands from the repository root:

```powershell
python -m venv webapp\.venv
.\webapp\.venv\Scripts\python.exe -m pip install -r webapp\requirements-lock.txt
Push-Location webapp\frontend
npm ci
npm run build
Pop-Location
.\webapp\.venv\Scripts\python.exe webapp\launcher.py start
```

Python 3.14 is tested. The installed Vite toolchain requires Node.js 20.19+ within 20.x, or 22.12+. The backend serves `frontend/dist`; Node is used to build or develop the interface.

**Stop Dashboard.cmd** requests a graceful companion shutdown. **Enable Auto Start.cmd** and **Disable Auto Start.cmd** manage the optional per-user watcher; see [Auto Start](docs/AUTOSTART.md). **Start Live Controls.cmd** is the older Guide's explicit live-start mode, separate from Remote's on-screen enable.

## Frontend development

For hot reload, stop the regular dashboard and start a development backend from the repository root. Allow the exact Vite origin explicitly:

```powershell
$env:DCS_DASH_HOST = '127.0.0.1'
$env:DCS_DASH_PORT = '18787'
$env:DCS_DASH_ARM = 'NO'
$env:DCS_DASH_ORIGINS = 'http://127.0.0.1:18787,http://localhost:18787,http://127.0.0.1:5173'
.\webapp\.venv\Scripts\python.exe -m webapp.backend
```

Then, from the repository root in another terminal:

```powershell
cd webapp\frontend
npm run dev -- --port 5173 --strictPort
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to the backend on port 18787. The regular launcher deliberately replaces inherited `DCS_DASH_*` settings, so use direct backend startup for this development configuration. It does not start the collector; live telemetry requires one separately running `collector.py`. This workflow stays on loopback and is not the phone-pairing launcher.

Build and check the frontend:

```powershell
cd webapp\frontend
npm run build
npx playwright test
```

The default local browser tests use installed Chrome and a preview server on port 4173. CI uses Playwright Chromium. The `*.spec.ts` tests use synthetic fixtures; machine-specific live-capture scripts and their reports are not bundled. See [development checks](docs/DEVELOPMENT.md) for CI and optional local integration checks.

Run the backend suite from the repository root, or use **Run Checks.cmd**:

```powershell
.\webapp\.venv\Scripts\python.exe -m pytest -c webapp\pytest.ini webapp\tests -q
```

## Local data and maintenance

The telemetry path is `Export.lua → UDP 127.0.0.1:17781 → collector.py → state/state.json`. The backend reads that state; it does not start a second telemetry listener.

Supported binding catalogues and available navigation sources refresh from the selected installation/profile. This reads your existing assignments rather than assigning keys. Local navigation coverage depends on source availability; optional pydcs fallback data is not bundled.

Generated `runtime/` and `data/`, the Python environment, frontend dependencies/builds, test output, and verification captures are local artifacts. Keep personal paths, bindings, credentials and flight logs out of commits. The public browser shell may be cached on supported origins; live API state is not an offline replay source. Refresh an open page after a frontend update.

## Guides

- [Installation and exporter chaining](docs/INSTALL.md)
- [DCS/profile discovery](docs/SETUP.md)
- [Navigation data and limitations](docs/NAVIGATION.md)
- [Remote panels and workflows](docs/REMOTE.md)
- [Native display setup and restore](docs/DISPLAY_FEEDS.md)
- [Pairing and connection recovery](docs/CONNECTIONS.md)
- [API/data schema](docs/SCHEMA.md)
- [Validation history](docs/VALIDATION.md)

Detailed repair history and test evidence belong in the validation documents, rather than the setup steps above.
