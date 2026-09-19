# DCS Companion

An experimental Windows companion for DCS World: a local navigation scope, aircraft binding reference, cockpit display viewer, and phone or tablet controls. It runs on your DCS computer and works without Internet access after setup. No AI service or cloud account is required.

## What it does

- **Navigation Scope:** ownship, airfields, navaids and available routes, with North up / Track up and independent layer controls. Selecting an airport or aircraft updates the same **DIRECT TO · DISPLAY ONLY** strip below the scope. Contact readouts include distance, true bearing, heading, ground speed and altitude; phones bring the strip into view after selection.
- **Mission Awareness:** separate Friendlies and Enemies layers for verified single-player mission positions. Direction arrows follow ground track, falling back to heading when available. This is mission-coalition information, not radar detection or IFF. It requires a compatible local mission bridge in addition to the telemetry exporter.
- **Hornet displays and bezels:** Left DDI, AMPCD and Right DDI appear in cockpit order on desktop and stack with their bezel buttons on a phone. Native display streams and exported text are separate sources. Images require explicit monitor-export and capture setup.
- **Phone controls:** pair on a trusted local network, then choose **Connect controls → Enable live controls** on the phone. Supported momentary UFC, DDI and AMPCD buttons use your existing bindings. Workflow editing stays on the PC.
- **Binding and flight reference:** supported F/A-18C and F-22A catalogues resolve installed defaults and your saved assignments. Cockpit text, flight data, source freshness and connection diagnostics remain available in their own views.

These are the current v26 browser features. Aircraft support is strongest for the Hornet; generic display layouts can accommodate additional exported displays, but do not imply working video, bindings or cockpit controls for every aircraft. Missing data stays unavailable.

## Requirements

- A 64-bit Windows computer with DCS World and the aircraft/terrains you intend to use installed.
- Python **3.14** is the tested version. The setup script uses `python` from your PATH.
- Node.js **20.19+ within 20.x, or 22.12+**, with npm available on PATH, for the frontend build.
- A modern browser; a phone or tablet on the same trusted LAN is optional.
- Internet access for dependency installation. Ordinary use reads local DCS sources and does not need online map tiles.

DCS game files, aircraft modules, terrain assets and display drivers are not bundled. Navigation coverage depends on available local DCS sources and, for some fallback positions, an optional local pydcs installation. A fresh checkout does not include another user's generated terrain or binding caches.

## First run

1. Download or clone the source into a writable folder, keeping the repository layout intact.
2. Run **`webapp/Setup Dashboard.cmd`**. It creates `webapp/.venv`, installs the locked dashboard dependencies, runs `npm ci`, and builds the browser interface.
3. Follow the [installation guide](webapp/docs/INSTALL.md) to install and chain the telemetry exporter in your selected Saved Games profile. This is a separate manual step; the setup script does not install it. Preserve existing exporters.
4. Run **`webapp/Start Dashboard.cmd`** and open [the local dashboard](http://127.0.0.1:18787).
5. Open **Your setup** on the DCS computer and confirm the discovered installation and profile. Enter a cockpit for live telemetry, or browse available local reference data with DCS closed.

The launcher starts or reuses the collector; `collector.py` owns UDP `127.0.0.1:17781`. Do not run `listener.py` alongside it. Use **`webapp/Stop Dashboard.cmd`** to stop the companion.

For optional automatic launch, see [Auto Start](webapp/docs/AUTOSTART.md). For installation, profile selection and optional integrations, start with the [installation guide](webapp/docs/INSTALL.md).

## Use a phone or tablet

Stop the loopback dashboard if it is already running, then run **`webapp/Start Tablet Dashboard.cmd`** and choose the computer's private network address. Open **Pair phone** on the PC and scan the QR code. Pairing does not enable aircraft inputs.

In **Remote**, explicitly connect and enable live controls when ready, keeping DCS focused on the PC. Preview/dry-run is the default. Reloading does not replay an action or reconnect live controls automatically; backend restarts require pairing again. The older Guide's startup live setting is separate from Remote's per-screen enable.

LAN access uses local HTTP and is intended for a trusted network, without Internet port forwarding. Firewall setup is a separate step. See [pairing and connections](webapp/docs/CONNECTIONS.md) and [Remote controls](webapp/docs/REMOTE.md).

Native Hornet images additionally require a prepared DCS export layout and enabled capture. A virtual display driver is an optional external installation; a dedicated physical export monitor is another route. Follow the [display setup guide](webapp/docs/DISPLAY_FEEDS.md).

## Optional MCP interface

`mcp_server.py` is an independent stdio interface for an MCP client. It reads the same collector state and uses the existing guarded input sender; the browser dashboard does not require it.

After dashboard setup, install the separate MCP dependencies from the repository root:

```powershell
.\webapp\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\webapp\.venv\Scripts\python.exe .\mcp_server.py
```

Configure your MCP client with the absolute interpreter and script paths for your checkout. Keep one collector running; MCP does not own the UDP port. MCP actions need the legacy binding index described in the [installation guide](webapp/docs/INSTALL.md), and input calls default to dry run.

## Repository layout

| Path | Purpose |
| --- | --- |
| `Export.lua` | Telemetry and cockpit-text exporter source |
| `collector.py`, `listener.py` | Shared state collector and alternative diagnostic listener |
| `input_sender.py`, `mcp_server.py` | Guarded Windows input sender and optional MCP interface |
| `tools/build_index.py`, `tools/dump_binds.lua` | Legacy binding-index tooling |
| `webapp/backend/` | FastAPI application and local DCS integration |
| `webapp/frontend/` | React/TypeScript interface and browser tests |
| `webapp/tests/` | Backend regression tests |
| `webapp/docs/` | Setup, feature guides and validation history |

Generated state, logs, local paths, controller identities, caches and captures belong to the local installation, not the source package. See [dashboard development](webapp/README.md) for builds and tests, and [validation history](webapp/docs/VALIDATION.md) for the distinction between automated checks and live observations.

This is a private development repository. No open-source license has been selected.
