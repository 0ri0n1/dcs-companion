# Data contracts

## Collector boundary

The existing `dcs-copilot/state/1` file remains the app's telemetry boundary. The exporter is unchanged; the existing single collector now adds independent display receive/identity evidence and complete packet capture. Ownship effective age is `stream.data_age_seconds + max(0, now - written_epoch)`. Each display requires its own `received_epoch_by_id`, `age_seconds_by_id`, `identity_by_id` and `display_session`. Aggregate display age is insufficient to establish a particular page as current. Future timestamps, malformed positions, missing process identity, paused/nonadvancing model time and missing cockpit state prevent command execution.

`export_capture.latest` preserves full decoded packets indexed by type (and ID for indications), with receive time and collector aircraft/session identity. Its memory is bounded to 128 entries/4 MiB and evictions are counted. Latest entries clear across exporter/aircraft/model-reset boundaries. The collector also archives complete received JSON packets in four rotating 16 MiB JSONL files; errors are reported without interrupting collection. This cannot recover packets lost by UDP or fields trimmed/omitted in Export.lua. Complete file-based capability probes are read only when their aircraft, probe number, wallclock, model time and settled state match the current received probe.

Exporter hello/bye counters, DCS PID/creation time, collector generation and model-time reset form a derived session guard. This is not an authoritative mission title/hash. A configured mission archive remains a planning document until its mission-member SHA256 matches an explicit live mission identity. Runtime validation must exercise same-aircraft mission changes and UDP lifecycle loss before extending command capability.

## Navigation database

`webapp/data/navigation/manifest.json` indexes `dcs-navigation/1` caches by DCS version and canonical terrain. DCS version is read from `autoupdate.cfg`. Each cache has `airfields`, `navaids`, `quarantine`, `projection` and `provenance`.

Every source record records original path, SHA256, source modification time, extraction time/method, DCS version, terrain, units, coordinate reference, confidence (`verified`, `derived`, `community`, `unknown`) and whether a mission overlay replaced it. Nested airport position provenance can differ from its radio/beacon provenance. Community airport reference points are explicitly labelled and never promoted to DCS-verified runway thresholds.

Airfields and standalone navaids are distinct. Runway pavement geometry, localizer front course and measured Batumi centerline correction are separate values. Unknown coordinates, pavement lengths, elevations and headings remain JSON `null` or absent, rendered Unknown; they are never replaced by zero or a beacon position. Malformed values and duplicate identifiers are quarantined with reason and provenance.

Mission overlays use `dcs-mission-navigation/1`: mission-member hash, theatre, date/time, weather, player/client routes, own-coalition navigation support objects, beacon/ICLS tasks, and provenance. Initial object positions are planned positions, never live radar tracks. The constrained parser accepts literal data tables and rejects code/function execution, duplicate keys, oversized/deep inputs and unsupported expressions.

The generic engine uses great-circle distance/bearing, ground speed for ETA, explicit TRUE/MAG labels, cross-track geometry, reciprocal headings, wind components, destination points, approach and descent geometry. Static localizer data cannot create runway guidance when pavement geometry is unknown.

## Binding cache

`dcs-companion/bindings/1` stores aircraft, DCS version, generation time, watched-source fingerprints, devices keyed by identity/GUID, actions, conflicts, contextual overlaps and a very small safe-action catalog. Each action preserves its DCS command identifier, device, category, merged combo, default/user source and collision information. Source truth is installed module/shared defaults plus per-device Saved Games diffs; HTML exports are never read.

`webapp/data/bindings/f22-allowlist.json` follows the existing sender's `actions` and `by_combo` structure. It is separate from legacy `binds/binds.json`. Source changes immediately invalidate command availability. No unsupported aircraft inherits the F-22 action catalog.

## Browser API

All routes are same-origin. Every protected read requires a paired cookie; every mutation requires an exact configured Origin. Host checks reject DNS-rebinding hostnames. No route accepts an arbitrary filesystem path, key combo, shell command or Lua program.

| Route | Purpose |
|---|---|
| `GET /api/bootstrap` | Public minimal mode information; no live state |
| `POST /api/session` | `{token}` or `{ticket}` creates an HttpOnly/SameSite session; QR tickets are single-use; loopback can pair locally |
| `POST /api/pairing-ticket` | Authenticated local-PC browser creates a 120-second single-use QR invitation; LAN mode required |
| `GET /api/diagnostics` | Authenticated bounded connection/health event history; no credentials or packet contents |
| `GET /api/state` | Full current dashboard snapshot |
| `GET /api/events` | Authenticated SSE snapshots; reconnection never enqueues work |
| `GET /api/bindings?aircraft=...` | Active catalogue by default; explicit F-22A or FA-18C_hornet selection |
| `GET /api/setup?refresh=true` | Authenticated selected/active setup, detected candidates, inventory and binding counts |
| `POST /api/setup/selection` | `{install_id, profile_id}` from detected candidates; loopback only, saves companion preference for next restart |
| `GET /api/export/archive/{index}` | Authenticated JSONL archive download, fixed indices 0–3, newest first |
| `GET /api/navigation/{terrain}` | Explicit offline terrain library, no ownship |
| `POST /api/commands` | `{request_id: UUID, action_id, confirmed: true}` only |
| `POST /api/commands/{id}/cancel` | Immediate cancellation while pre-send |

All API responses use `Cache-Control: no-store`. Only public application-shell assets enter the service-worker cache. The top-level snapshot contains `server`, `health`, `aircraft`, `bindings`, `binding_catalogs`, `adapters`, `adapter`, `navigation`, `guide`, `smart`, `sensors`, `cockpit`, `mission`, `awareness`, `export_data` and `queue`. Unverified raw sensor/world fields remain diagnostic exports; they never become onboard sensor tracks. Current export denials withhold corresponding browser fields. No denial is bypassed with cockpit text.

`awareness` is the user-requested, explicitly separate single-player mission-awareness feed. Its fixed read-only local bridge queries confirm single-player and current ownship identity, then return live opposing-coalition units. Each result carries mode, source, age, aircraft/session identity, coverage counts and contacts with geographic coordinates. It never populates `sensors.contacts`, changes command context or activates a planned mission overlay. Unsupported/unknown multiplayer state, stale observations and identity mismatches return no contacts. The public API accepts no Lua, host, coalition selector or mission-object query. See `AWARENESS.md` for exact collection and validation rules.

`cockpit.displays` contains ID, aircraft-specific label where verified, status, individual age, `elements` and explicitly historical `last_observed_elements`. Text is rendered as escaped text, never HTML. No image/video stream is supplied. `sensors` extracts a limited set of locally defined Hornet FLIR/laser strings, while radar/RWR contact arrays remain empty pending validation. `export_data.groups` carries source, age, status, data and diagnostic detail without inventing meanings for unknown fields.

`smart` provides plain-language `status`, `title`, `detail`, `binding_refresh`, `navigation_refresh`, and a bounded public `autostart` heartbeat summary. Refresh statuses are Current, Waiting, Refreshing, Unavailable or Error. Navigation refresh includes a per-backend `generation` counter so browser libraries reload after publication. Navigation is unavailable until the first successful source check, during invalidation, and after failed refresh. Browser reconnection reauthenticates automatically only on exact loopback hosts; LAN pairing remains deliberate. No reconnect operation resubmits a command.

## Queue and audit

SQLite (`runtime/commands.sqlite3`) stores canonical UUIDs, original context, resolved action, state history, expiry, attempted flag and sender/audit evidence. A partial unique index allows only one active request. `synchronous=FULL` persists the attempt claim before the sender is called. An ambiguous send or interrupted process consumes the request forever; no automatic retry exists.

`Idle → Selected → Queued → Awaiting DCS Focus → Settling → Revalidating → Sent → Confirming → Confirmed / Unconfirmed / Rejected / Expired / Cancelled`.

Before the sender call, process, aircraft, mission/session, binding generation, exact combo, freshness, advancing model time, focus and benign classification must still match. The original `KeySender` adds its existing scan-code, focus, allowlist, rate and audit guards. An actual Sent state requires a successful sender result plus matching audit evidence. Dry run produces a single original-format audit line and never enters Sent. A validated adapter-specific confirmer is required for Confirmed; none is enabled for the F-22 MVP.

The database is also the queue transition audit. `state/input-audit.log` remains the input-delivery audit. Its offset and matching line are linked from the queued request without adding a second sender entry.
