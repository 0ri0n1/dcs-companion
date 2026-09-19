# Aircraft and binding capabilities

Validation of this application: **Static checks passed** for the tested F-22 and F/A-18C binding catalogs and isolated adapter contracts. **Not yet flight-tested**. No live inputs were sent to DCS during the build. No `Live ground integration passed` or `Flight-tested` claim is made.

## Capability matrix

| Aircraft | Installed detection | Browser binding/command status | Cockpit programming | Sensor contacts | Runtime confirmation |
|---|---|---|---|---|---|
| F-22A | Installed `Mods/aircraft/f-22a` | Defaults + user diffs resolved; three benign selection actions | TACAN, radio, arbitrary waypoint entry, sequences and AP are Unverified and disabled | Unverified; no tactical display | None; commands remain Unconfirmed |
| F/A-18C | Installed full-fidelity module | Current defaults + Hornet user diffs; Remote resolves 84 momentary panel buttons, withholding missing/conflicting bindings | UFC, DDI and AMPCD remote buttons; other measured tools remain references | Field availability depends on eligible fresh export; no world-object fallback for sensors | Remote preview verified; physical cockpit effects await pilot verification |
| F-15C | Installed module checked at runtime | Not yet flight-tested; no browser commands | Unverified | Unverified | None |
| F-23A | Installed mod checked at runtime | Not yet flight-tested; no browser commands | Unverified | Unverified | None |
| VSN F-35 variants | Installed mod checked at runtime | Not yet flight-tested; no browser commands | Unverified | Unverified | None |
| Unknown or uninstalled type | Exact identifiers required | Unsupported; empty bindings and action list | Unsupported | Unsupported | None |

MEASURED (local filesystem): this installation contains the VSN F-35 mod, so it is listed as installed but unverified. The adapter recognizes the exact `VSN_F35A`, `VSN_F35A_AG`, `VSN_F35B`, `VSN_F35B_AG`, `VSN_F35C`, and `VSN_F35C_AG` identifiers declared by its installed entry file. Presence is not an avionics validation.

Shared navigation calculations and fresh ownship plotting do not require a cockpit-programming adapter.

The user-requested **Mission awareness** layer is separate from the sensor-contact column above. It can display live opposing-coalition units for a verified single-player ownship across aircraft types, using mission state rather than an avionics adapter. These markers are not evidence of radar detection, IFF or sensor support. Multiplayer and unknown mode are unavailable. See `AWARENESS.md`.

## Binding truth and extraction

MEASURED (local source inspection): the F-22 keyboard and joystick defaults inherit the matching `Config/Input/Aircrafts/base_*_binding.lua` and `common_*_binding.lua`. `Scripts/Input/DefaultAssignments.lua` supplies device-specific assignment macros. `Scripts/Input/Data.lua` specifies wizard override precedence, template/default selection and ignored feature handling. These actual inputs are evaluated in the companion's constrained Lua environment. No DCS runtime, socket or key sender is invoked by extraction.

The implementation extends the existing `tools/dump_binds.lua` technique and imports `tools/build_index.py` for canonical modifier ordering and combo parsing. The old dumper's permissive unresolved proxies would omit automatic device assignments; the companion evaluator resolves those from the installed assignment file and fails on unknown globals. DCS engine command symbols stay symbolic where numeric values are unavailable; Saved Games command IDs are retained. Merge identity uses exact names, matching the existing merge tool, with user-only actions explicitly marked unverified and never command-eligible.

Effective assignment: module/default combos minus matching Saved Games `removed`, plus `added` and `changed`. Axis `changed` entries preserve filter information and are reapplied as assignments, matching the installed `Data.lua` loader. `ignore_features` is honored. Global UI/voice bindings participate in same-device conflict checks. Command-menu collisions are reported as context-dependent overlaps, because the menu changes what the F keys do. DCS's generic `Aircrafts/Default` profile is a fallback, not a second active aircraft profile; it is watched for changes but never overlaid onto the selected module defaults.

HTML under `InputLayoutsTxt` is never read, merged, watched, or treated as authority. Changing an HTML fixture has no effect on the binding fingerprint.

## Device identity and coverage

MEASURED (local files, refreshed September 18, 2026): the F-22 cache resolves 2,508 action/device rows, 494 bound rows, four device columns, zero direct conflicts and twelve command-menu contextual overlaps. These counts are build-specific, not hardcoded application limits.

| Device | Identity/source | Representative resolved assignment |
|---|---|---|
| Keyboard | `F-22A/keyboard/Keyboard.diff.lua` + shared defaults | Next selection = left Ctrl + grave; previous = left Shift + grave; nav mode = `1` |
| T.Flight Hotas One | GUID `51993E00-9A4E-11F1-8002-444553540000`; F-22 per-device diff | Own GUID-scoped key and axis assignments, including axis filters |
| Razer Tartarus Pro | GUID `D05E6650-B121-11F0-8018-444553540000`; F-22 per-device diff | `ICP ALT` = `JOY_BTN8` |
| Razer Xbox 360 Controller | GUID `D147B4E0-B121-11F0-801C-444553540000`; latest DCS log device census | No F-22 diff; installed generic controller defaults apply |

Connection state is **Unknown**, since a saved profile and last-seen DCS log entry are not live hardware evidence. The inventory includes the actual GUIDs from saved profiles and evidenced DCS `INPUT created … full id` records. It does not claim a verified Windows Raw Input listener. Physical input highlighting is **Unavailable**.

Log rotation does not erase an observed device column. Current `dcs.log` and `dcs.log.old` observations are retained in each aircraft's own `observed_devices` ledger, with source path, SHA-256 and observation time; device rows link their evidence. Earlier catalogs can recover identities from at most sixteen recent, fingerprint-valid backups of that same aircraft catalog, with the backup hash and original census provenance recorded. This recovery copies no assignments or commands. Every retained device's controls are resolved again from the selected aircraft's installed defaults and current Saved Games diff. An isolated build with no log or cached observation does not invent missing devices. Removing a saved diff returns the device to evidenced defaults where history exists; it does not claim that the device is physically present.

Tartarus DCS joystick controls and Synapse physical keycaps are separate. MEASURED: `JOY_BTN8` is bound to `ICP ALT` in the actual F-22 diff. Its physical key number, keyboard-emission mappings, active Synapse profile, dual-function depth and Hypershift layer are **Unknown**. No Hornet Tartarus mapping is applied to the F-22. MEASURED: the installed DCS assignment table gives this Tartarus an auxiliary-panel default with no automatic flight axes; the cache preserves that distinction.

## Independent F/A-18C catalog

The exact installed input directory is `E:\DCS World\Mods\aircraft\FA-18C\Input\FA-18C`; user assignments come from `C:\Users\<user>\Saved Games\DCS\Config\Input\FA-18C_hornet`. The evaluator follows the installed Hornet `devices.lua` and `command_defs.lua`, shared keyboard/joystick defaults, device assignment macros and Supercarrier keyboard definitions. All evaluated default dependencies are covered by source fingerprints. Saved keyboard and joystick diffs, custom modifiers, global input layers and the normalized last-seen device census are watched independently from the F-22 catalog. Neither catalog reads legacy `binds/binds.json` as current truth.

MEASURED (DCS 2.9.29.27468, September 18, 2026): the Hornet cache contains 5,963 action/device rows, 490 bound rows, five device columns, two direct conflicts and twelve command-menu contextual overlaps. The configured devices are Keyboard, T.Flight Hotas One and Xbox One controller GUID `FFE2A4A0-B1AC-11F0-8004-444553540000`. The Razer Xbox 360 controller and Tartarus are present only in the last-seen DCS census and use installed defaults. No Hornet Tartarus diff exists in this installation; physical keycap mappings and connection state remain Unknown.

Current examples resolved directly from these sources:

| Hornet action | Keyboard | T.Flight Hotas One |
|---|---|---|
| RAID/FLIR FOV Select Button | `I` | `MOD1+JOY_BTN8` |
| LTD/R Switch - ARM | Unbound | `MOD1+JOY_BTN4` |
| Throttle Designator Controller - Up | `;` | `JOY_BTN_POV1_U` |
| Throttle Designator Controller - Depress / DEPRESS | `Enter` | `JOY_BTN5` |
| Sensor Control Switch - Fwd | `RAlt+;`, `LCtrl+LAlt+B` | `MOD1+JOY_BTN_POV1_U` |
| FLIR Switch - ON / STBY | Unbound | Unbound |

`MOD1` is the saved HOTAS momentary modifier `JOY_BTN9`; `MOD2` is `JOY_BTN11`. Those are DCS button identifiers, with no inferred physical labeling. The current direct conflicts are keyboard `Back` (two control-stick visibility actions) and `LCtrl+LShift+Y` (AMPCD PB 16 versus global Chat show/hide). They are reported without modifying profiles.

Hornet files are `webapp/data/bindings/f18-snapshot.json` and `f18-allowlist.json`; the latter remains empty for the older Guide. Hornet binding snapshots retain `display_only: true` for that reference/Guide path, and rows carry `aircraft: FA-18C_hornet`. The separate Remote service validates exact momentary panel commands and current binding fingerprints, generating an isolated runtime allowlist for each attempt. Browsing Controls never actuates the aircraft. See [Remote](REMOTE.md) for enable, focus, session, stop and replay rules.

## Safe action allowlist

| Adapter action ID | Exact DCS action name | Initial resolved combo | Observable result |
|---|---|---|---|
| `f22.nav.next` | Next Waypoint, Airfield Or Target | `LCtrl+grave` | Unverified; selection depends on current mode |
| `f22.nav.previous` | Previous Waypoint, Airfield Or Target | `LShift+grave` | Unverified; selection depends on current mode |
| `f22.nav.mode` | (1) Navigation Modes | `1` | Unverified; cycles mode from potentially unknown initial state |

The index `webapp/data/bindings/f22-allowlist.json` uses the existing `KeySender` schema. Each allowlisted action has exactly one verified, conflict-free keyboard combo. All other controls are display-only. The original `binds/binds.json` is untouched and cannot be inherited by this F-22 command path.

Command eligibility additionally verifies the numeric DCS command identity against the installed F-22 `command_defs.lua` and `ICP_System.lua`: next = 102, previous = 1315, navigation mode = 105 on this build. A diff with a matching name but a mismatched command ID cannot authorize a send. These values are extracted rather than hardcoded as generic FC-family controls.

Source SHA-256s are recomputed before sending. A changed file, removed file, newly added diff, new device identity, changed assignment macro or resolver change invalidates the cache and returns no safe actions. No Lua evaluation occurs in that pre-send check. Rebuild the F-22 catalog explicitly with `python webapp/tools/build_bindings.py`; add `--aircraft FA-18C_hornet` for the independent Hornet catalog. Backend maintenance refreshes each catalog separately. Generated cache replacement keeps content-addressed backups. A source change during extraction aborts publication.

The device census is a special dependency: the fingerprint hashes the normalized set of current and retained observed input-device identities, not unrelated flight log output. A new identity invalidates the catalog; an empty or rotated log does not. Source records state this method. DCS version is read from `autoupdate.cfg`.

## API and future work

`BindingService(..., aircraft='F-22A')` preserves the original default; `aircraft='FA-18C_hornet'` selects the separate Hornet catalog. `snapshot()` supplies the owned catalog; an explicit different aircraft returns Unsupported with empty rows. `get_safe_actions()` fails closed on unsupported aircraft, invalidated sources and every Hornet catalog. `check_current()` is read-only. `refresh()` writes only companion caches. `get_adapter(name)` uses exact identifiers; `list_adapters()` returns the capability matrix. `can_confirm(...)` currently returns false for every aircraft/action; arbitrary replay transitions cannot establish a confirmed F-22 command.

Next safe steps: instrument a single-player F-22 ground session, validate one navigation selection and trustworthy display readback, then fly the separate checks documented in the main validation report. Integrate Hornet tools as a full-fidelity adapter using its own binding hashes, measured display maps and multi-sample cue verification. Resolve and flight-test F-15/F-23/F-35 independently before exposing any cockpit commands. Add a verified read-only Windows physical-device listener and measured Synapse mapping only with evidence.
