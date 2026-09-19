# First-machine installation

This is a Windows source distribution. It contains the companion and its own
telemetry exporter, not DCS, module files, terrain caches, drivers, or a configured
Saved Games profile. Python 3.14 is the tested interpreter; use Node 22.12 or newer
within Node 22 for the frontend. Initial dependency installation needs Internet.

## Build the companion

1. Clone or unpack the repository to a writable folder. Keep the root files and
   `webapp` together; the dashboard needs the collector and input sender above it.
2. Run `webapp/Setup Dashboard.cmd`. It creates `webapp/.venv`, installs the pinned
   Python requirements, runs `npm ci`, and builds the frontend.
3. For community airport positions and map projections, install the optional
   navigation source into that same environment from the repository root:

   ```powershell
   webapp\.venv\Scripts\python.exe -m pip install -r requirements-navigation.txt
   ```

   The companion reads the package's airport/projection files as data; it does not
   execute the package to build navigation. These positions retain community
   provenance. Without them, missing positions remain unknown. Installed DCS
   terrain source files provide available beacon and radio data. No generated
   catalogues are included in Git.

## Add telemetry without replacing other exporters

**Close DCS before changing its export scripts.** Select the Saved Games profile
you actually launch, such as `Saved Games/DCS` or your named profile. Do not assume
that its path is the same as the DCS installation directory.

1. Back up the profile's existing `Scripts/Export.lua`, if present.
2. Create `Scripts/dcs-companion` inside that profile. Copy this repository's root
   `Export.lua` into that new folder as `Scripts/dcs-companion/Export.lua`.
3. Keep every existing line in the profile's top-level `Scripts/Export.lua`. Append
   this one loader line **once**, after existing exporter loaders. If the file does
   not exist, create it with this line:

   ```lua
   dofile(lfs.writedir() .. 'Scripts/dcs-companion/Export.lua')
   ```

The companion exporter captures the existing `LuaExport*` callbacks when loaded
and chains them. Test your particular combination of export extensions after
installation. Do not copy a previously deployed personal export configuration into
this repository.

Run `webapp/Start Dashboard.cmd`, then enter a cockpit. The launcher starts or
reuses the collector; **only one collector or listener may own UDP 17781**. The
dashboard reads `state/state.json` and serves `http://127.0.0.1:18787`.

If discovery has multiple candidates, choose the correct installation and Saved
Games profile in **Your setup**. Explicit overrides are also available:

```powershell
webapp\.venv\Scripts\python.exe webapp\launcher.py start --install-root "D:\Games\DCS World" --saved-games "D:\Profiles\DCS"
```

Check **Data Health** for current ownship and display ages. A running collector
without a current cockpit is not proof that export data is live.

To remove this exporter, close DCS, remove only its loader line, and remove only
`Scripts/dcs-companion/Export.lua`. Keep the other contents of `Scripts/Export.lua`.

## Phone access and cockpit pictures

Use `webapp/Start Tablet Dashboard.cmd` for an explicit local-network address.
Open **Pair phone** on the PC and scan the QR code. Pairing lasts until the server
restarts. Preview/dry run is the starting mode; a paired phone can connect controls
and enable live controls deliberately for the current flight.

Local HTTP is intended for a trusted private network, not Internet exposure.
See [connections](CONNECTIONS.md) and [remote controls](REMOTE.md).

Cockpit pictures need an approved DCS export-monitor layout in addition to the
text exporter. Use **Your setup** and [display feeds](DISPLAY_FEEDS.md). The
repository does not include a virtual-display driver. The low-level virtual-display
installation helpers expect externally prepared driver/installer files and a
display-layout checkpoint; they are not a complete driver download installer.
An existing compatible virtual display or dedicated physical export display can
be configured separately. Apply DCS layout changes only while DCS is closed.

## Optional mission bridge

Mission Awareness, live mission metadata, and registered mission scripts require
a compatible, authenticated local DCS Fiddle bridge installed separately. The
companion detects the existing profile hook; this source package does not install
or distribute that hook, its credentials, or a desanitized mission environment.
Without it, those features report unavailable. Ordinary ownship telemetry and
cockpit display text use the companion exporter above. Mission Awareness remains
single-player only and does not represent radar or IFF detections.

## Optional MCP server

Install the optional requirements into an environment for the stdio server:

```powershell
webapp\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
webapp\.venv\Scripts\python.exe mcp_server.py
```

Normally an MCP client launches that command. The server reads the same collector
state; it does not bind the telemetry port. Its legacy root binding index is
separate from the dashboard's generated catalogues. Create your own index with
`tools/dump_binds.lua` and `tools/build_index.py` before using MCP input commands;
no personal keyboard/controller index is shipped. Telemetry reads do not need an
index. Consult each tool's usage and keep dry run enabled for initial validation.
