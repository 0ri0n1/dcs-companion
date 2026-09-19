# Navigation data and validation

The Navigation Scope reads the existing collector state file. It owns no UDP socket, writes no DCS configuration, executes no mission Lua, and sends no cockpit commands. Static data remains available without DCS or the network.

## Example installed data coverage

The following counts describe an earlier local validation, not bundled data. A new checkout generates its own libraries from its installed DCS sources and optional `requirements-navigation.txt` package.

The build read DCS version **2.9.29.27468** from `E:\DCS World\autoupdate.cfg`. The cache directory is `webapp/data/navigation/<DCS version>/`; `manifest.json` selects the versioned files. Rebuilds back up an existing file before replacing it.

| Terrain | Beacon records | Standalone navaids | Beacon-equipped fields | Radio rows | Airfield records | Positioned fields | ATC frequencies | Verified runway geometry |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Caucasus | 164 | 64 | 19 | 21 | 21 | 21 | 84 | Unknown |
| Persian Gulf | 101 | 25 | 27 | 27 | 29 | 29 | 26 | Unknown |
| Mariana Islands | 19 | 3 | 4 | 5 | 8 | 8 | 20 | Unknown |
| Marianas WWII | 0 | 0 | 0 | 11 | 11 | 0 | 44 | Unknown |

Airfields are joined by `airfield<ID>_<facility>` identifiers, never by all distinct beacon display names. Caucasus has 21 physical airfield records; its 64 standalone navaids remain separate. Beacons are not airport reference points.

The readable Beacon/Radio files lack airport reference coordinates. The installed pydcs package supplies **community** airport reference points for three maps, read as Python syntax trees without importing or executing pydcs. Its projection parameters are also community data. Those coordinates have separate `position_provenance`; the DCS version attached to this provenance is the extraction context, not a claim that pydcs was built for that version. Reference-point projection was independently checked against 30 shipped beacon coordinate pairs, all within 2 metres. Additional pydcs-only fields remain community inventory. Modern Marianas points are never transferred to WWII.

All airport elevations, magnetic runway headings, runway lengths, widths, surfaces and thresholds remain `null`/`Unknown` until reliable data exists. The presence of a community airport point does not establish any runway geometry. Navaid elevations describe the antenna, not the airport.

Five Persian Gulf TACAN source frequencies are quarantined because they are not valid TACAN receiver frequencies: Havadarya (`airfield9_0`, 111 MHz), Kerman (`airfield18_0`, 122.5 MHz), Minhad (`airfield12_0`, 115.2 MHz), Shiraz (`airfield19_1`, 114.7 MHz), and Kochak (`airfield16_0`, 114.2 MHz). Source channels remain recorded. These values are not silently presented as valid tuning frequencies. A missing frequency is Unknown rather than malformed; channel-only RSBN/PRMG/TACAN records remain available.

TACAN receiver-frequency/channel conversion follows the installed `Scripts/World/Radio/BeaconTypes.lua` exactly. **1025–1150 MHz means Y**; 1005 MHz is 44X, and 1154 MHz is 67X. Every channel 1–126 in both bands is round-trip tested. Channel-only terrain TACAN X labels follow the installed Mission Editor `Mission/BeaconData.lua` `loadBeaconsMarkerT4` convention; these bands are explicitly `derived` and are not flight-verified. An existing valid receiver frequency takes precedence over that display convention.

Batumi preserves three distinct facts: localizer emission direction −54.415131°, derived front course 125.584869° TRUE, and the existing empirically measured rollout centerline 130.10° TRUE. The measured line retains the hash and description from `data/runway-overrides.json`. It does not create thresholds, a runway polygon, or a runway length.

## Record schema

`schema: dcs-navigation/1` caches contain `terrain`, `dcs_version`, `airfields`, `navaids`, `quarantine`, `projection`, and `provenance`.

Every navigation record and every ATC frequency includes provenance with:

- DCS version and terrain.
- Absolute original source path, SHA-256 and source modification time.
- Extraction timestamp and method.
- Units and coordinate reference.
- Confidence: `verified`, `derived`, `community`, or `unknown`.
- `mission_overlay_replaced`.

`verified` means verified against the cited source file or API result, not flight-tested. Nested derived or community fields have their own provenance. A record-level source does not upgrade all nested unknowns to verified.

Airfields use stable IDs such as `Caucasus:airfield:22`, retain numeric DCS IDs, names, callsigns, reference latitude/longitude, DCS local position, ATC frequency records, associated navaids and runways. Each frequency supplies value, unit, Hz, modulation, purpose and band. Missing modulation is `Unknown`; it is not guessed from aircraft defaults.

Navaids have their own IDs, geographic/local coordinates, antenna elevation, facility type, callsign, frequency, channel/band, associated airfield ID if present, original signed direction and separate localizer-front-course fields. Standalone facilities have no airfield ID.

Malformed coordinates, impossible headings/elevations/lengths/widths, invalid frequencies and duplicate IDs are quarantined with their original source identity and reason. No questionable coordinate or heading is normalized into verified data. Valid DCS signed localizer angles are stored unchanged; the reciprocal is an explicitly derived field.

## Safe Terrain API export and import

`tools/export_terrain_navigation.lua` is a one-time exporter for a **trusted DCS/Mission Editor Lua context in which the selected terrain and `terrain` module are already loaded**. It installs no hook and is never loaded by the dashboard or a mission. It calls `Terrain.GetTerrainConfig('Airdromes')` and `Terrain.getRunwayList(airdrome.roadnet)` and uses the engine coordinate conversion and height API. It writes a new timestamped JSON file only to `webapp/data/navigation/imports/`, refusing overwrite. It reads the current version from `autoupdate.cfg`. Create the imports directory first.

In an already available trusted developer Lua console, the explicit one-time invocation is:

```lua
local export = assert(loadfile('C:/src/dcs-companion/webapp/tools/export_terrain_navigation.lua'))()
print(export())
```

This is an operational integration point, **Not yet flight-tested** and not runtime-executed during this build. A normal mission DO SCRIPT environment generally lacks these APIs; do not desanitize mission scripting, replace `Export.lua`, edit DCS files, or install an unreviewed hook to make this work. If no trusted runtime console is already available, the export is **Blocked** on that runtime access and geometry stays Unknown. No proprietary terrain package reverse engineering is used.

The exporter was syntax-compiled with the installed `luae.exe` without executing its body. Its runway-end field names were checked against installed `Mission/BeaconData.lua` and `me_managerDTC.lua`. Source API return values not present are omitted. Geographic bearings between exported runway ends become explicitly derived TRUE pavement headings; the original grid course is retained separately. Magnetic headings stay Unknown. End coordinates are the API runway-edge points; displaced-threshold information remains unavailable unless separately supplied by the engine.

Import a resulting dump from the repository root:

```powershell
webapp\.venv\Scripts\python.exe webapp\tools\import_terrain_navigation.py webapp\data\navigation\imports\<export>.json
```

The importer enforces schema, terrain and exact installed-cache DCS version. It validates both ends, length, width, geographic positions, elevation and courses. Inconsistent geometry is quarantined. Existing versioned cache data is backed up before replacement. Restart the dashboard to load imported geometry. Current caches contain no invented exporter results.

Rebuild static source data after a DCS update:

```powershell
webapp\.venv\Scripts\python.exe webapp\tools\build_navigation.py
```

Rebuilding produces a fresh static cache with Unknown geometry; retain and re-import only a Terrain API dump matching that exact DCS version.

## Automatic source refresh

The local `NavigationMaintenance` helper supports the dashboard's automatic maintenance loop. Its checks compare the installed DCS version, installed supported terrain inventory and actual SHA-256 content of every relevant Beacon/Radio, BeaconTypes, Mission Editor beacon-handler, community airport/projection and measured-override source. Added or removed terrain/source files invalidate the affected generation. Modification time or a new extraction timestamp alone never triggers a rebuild. Missing or corrupt cache files also invalidate the generation.

The maintenance controller waits for source stability and serializes rebuilds with its local process lock. Invalid navigation is withheld while rebuilding. All extraction happens in a temporary dashboard runtime directory; DCS and Saved Games remain read-only. The helper checks the complete input set before and after extraction, then publishes backed-up versioned cache files and the manifest last. A source change during extraction publishes nothing. A source change during publication prevents the refreshed service from being activated. Failed or unavailable sources remain unavailable until a successful rebuild; they are not relabelled verified.

On the same DCS version, only affected terrains are rebuilt. Unaffected cache files are preserved byte-for-byte, including imported Terrain API runway geometry. A changed terrain or new DCS version gets fresh static data and Unknown runway geometry; no old-version pavement geometry is silently transferred. Previous versions and overwritten cache/manifest backups remain available. Missing or corrupt cache files are rebuilt from the original sources, with the corrupt bytes backed up when present. Fully unchanged refreshes write nothing.

Automatic source refresh improves data readiness only. It does not establish active mission identity, resolve the overlapping Marianas maps from coordinates, verify wind direction conventions, discover unavailable runway thresholds, validate physical input mappings, or promote an aircraft capability to flight-tested. No live DCS probe or input is sent by maintenance.

## Mission overlays

`parse_mission(path)` accepts a local `.miz`, reads only named ZIP members into memory and never extracts members to disk. Limits: 512 MiB archive/total expanded size, 5,000 members, 16 MiB mission/warehouse members, 1000:1 compression ratio, one of each named member, bounded Lua tokens/depth. The Lua reader accepts literal tables, numbers, strings and booleans; function expressions/calls, executable suffixes, nonfinite numbers and duplicate table keys fail. Executable mission script *strings* remain inert text. Terrain preambles such as `dofile` are never run.

The overlay contains theatre identity, player/client routes, mission weather, warehouse ownership, FARPs, navigation ships/carriers, support aircraft and explicit ActivateBeacon/ActivateICLS assignments. Units unrelated to navigation are excluded. Route selection requires exact ownship unit-name and aircraft match. Visible support objects require an established own coalition; unknown coalition hides objects. Enemies are never rendered, and `raw_latest` world-object lists are never used. Positions are mission planned positions, with an explicit label; task declarations do not prove a transmitter is currently active. Trigger scripts dynamically creating objects/beacons are not executed or inferred.

A configured `.miz` is a **planned overlay** until the backend matches it to the current mission identity. The existing exporter does not supply that identity. Merely loading a file must not override an ambiguous live theatre. Red/blue warehouse assignments are mission initial ownership, not live airfield capture state.

Mission surface wind speed is retained. Wind direction is preserved as `dcs_direction_deg` with `from_true_deg: null` because runtime verification of its FROM/TO and geographic/grid reference is pending. No real-world weather is substituted. The runway advisory algorithm and wind components are tested, but the UI must show Unknown until a session-matched mission supplies trustworthy wind reference and runway geometry. Suggestions are advisory and never ATC clearance or confirmed active runway.

## Live calculations and detection

Explicit terrain identity is preferred; unsupported or conflicting identities fail closed. Geographic fallback is derived and limited to conservative Caucasus/Persian Gulf regions. Modern Marianas and WWII overlap, so coordinates alone return Unknown and no airfield list. Offline library selection is labelled separately and supplies no live ownship.

Ground speed comes from horizontal raw velocity or a 3–10-second geographic position-history interval using advancing DCS model time. IAS and TAS are never substituted. Local grid velocity does not masquerade as true track; true track is derived from geographic position history. Old-session history and implausible speed are rejected. A stopped collector cannot keep data fresh by leaving `stream.live=true` in a file: elapsed wall time since `written_epoch` is included. Stale ownship can be displayed as last-known, but ETA becomes Unknown.

Generic pure functions provide distance, TRUE bearing, reciprocal headings, direct-to geometry, signed cross-track error, ground-speed ETA, wind components, unit conversion, final approach fixes, glidepath deviation and top of descent. Approach math uses the existing Copilot along/cross conventions but requires real threshold/elevation inputs rather than borrowing a localizer antenna. Runway suitability remains Unknown without verified aircraft performance requirements.

## Verification status

**Static checks passed:** all four beacon files and all four radio files parsed; source-based inventories and provenance present; all 252 TACAN channel/mode combinations checked; representative Batumi ATC/TACAN/offset localizer checked; projected positions independently compared against 30 shipped beacon pairs; geometry and unit golden cases; theatre separation and Marianas ambiguity; nullable cold collector; stopped collector freshness; IAS exclusion; bounded mission parsing; own-coalition visibility; exporter syntax; importer backup/quarantine tests.

Two actual installed missions were safely read without executing Lua: `Lesson 5-Waypoint Navigation.miz` (Caucasus, one player route), and `Persian Gulf FA-18C Case I Carrier Landing.miz` (one player route and explicit support/beacon assignments). This is static file validation only.

**Live ground integration passed:** not claimed for this navigation module.

**Not yet flight-tested:** ownship against F10, GS/track/bearing/ETA, projection error in flight, actual route sequencing, session identification, wind direction convention, runtime Terrain API dump, mobile sources, and multiplayer export behavior. No sensor contact validation is claimed. The next safe step is an ordinary single-player F-22 sortie with recorded telemetry; no probe mission is required.

## Portable Terrain API exporter call

The optional exporter now accepts explicit paths. In an already trusted DCS/ME Lua context, load it and call the returned function with the existing output directory and DCS installation root. This tool does not install a hook or change mission sandbox permissions.

```lua
local export = dofile([[C:/src/dcs-companion/webapp/tools/export_terrain_navigation.lua]])
local filename = export([[C:/src/dcs-companion/webapp/data/navigation/imports]], [[D:/Games/DCS World]])
```

Replace both example paths with your own. Do not publish the resulting DCS-derived dump.
