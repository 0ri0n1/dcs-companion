# Read-only Hornet sensor display observations

The sensor endpoint can show exact ATFLIR display text from the current
`FA-18C_hornet`: tracking label, zoom, focus, pod mask label, designated range
label, laser status text and displayed LST/LTD/R codes. It supplies no FLIR video,
laser-emission boolean, enemy classification, radar/RWR contact list or world map.
No cockpit inputs are needed to collect this information.

`backend.sensors.build_sensor_snapshot(raw, health, now=None)` consumes the existing
collector state. It never opens a socket or queries a simulator API. Other aircraft
are `Unsupported`; the Hornet's labels are not inherited by the F-22 or another pod.

## What was actually observed

**MEASURED, 2026-09-18:** `dcs_status` established a live Hornet session before
bounded read-only sampling. Four snapshots, 1.2 seconds apart, had advancing
ownship model time and indication counters. Right DDI (display 3) exported
`INR AUTO`, `MASK`, zoom `1.0` / `Z1.0`, focus `0`, LST and LTD/R codes `1688`, and a
changing range label ending in `TGT`. The later sample range was `16.9 TGT`.
These are observations from that sampling window, not a permanent description of
the user's aircraft. The old running collector had only an aggregate display age;
those samples cannot independently establish each display's freshness.

The observations above describe a historical sampling window. Machine-local
captures and verification artifacts are not distributed with this source package.

**DOC, installed official Hornet indicator scripts:**

- `<DCS install>/Mods/aircraft/FA-18C/Cockpit/Scripts/Multipurpose_Display_Group/Common/indicator/Pages/MPD/FLIR/FLIR_AG.lua`, lines 22–45 in the inspected version, defines the range and laser status elements. The laser status enumeration includes `L ARM`, `M ARM`, `LTD/R`, `MARK`, `LTD` and `MASK`. A separate graphical cross is not a trustworthy text readout.
- The adjacent `FLIR_CMN.lua`, lines 105–151, defines tracking, zoom, focus, pod mask and code elements.

The implementation preserves these labels as text. `MASK`, an armed label, or a
displayed code is not a claim that a physical laser beam is being emitted. Missing
labels do not mean the laser is off. A `TGT` range label does not establish an enemy.
IR/NAR text was also seen, but anonymous indicator element names are not used as a
stable API and are excluded from the allowlist.

## Why radar and RWR contacts remain unverified

**DOC, installed DCS API reference** `<DCS install>/Scripts/Export.lua`
(line references describe the inspected version):

- Line 842 describes `LoGetSnares` as a chaff/flare inventory table. The existing
  copilot exporter counts that table's keys at `Export.lua:714–718` and names the
  result `sensor.rwr_count`. The observed value of 2 is not a threat count. This
  value is never read or exposed by the sensor snapshot.
- Lines 735 onward describe `LoGetTWSInfo` as **Threat Warning System** status,
  with `Mode` and `Emitters`. The existing exporter at lines 697–709 treats the
  top-level table as radar contacts. Its `tws` output cannot be represented as a
  validated Track-While-Scan radar contact feed.
- Lines 686–726 document a `laser_on` field under `LoGetSightingSystemInfo`, but
  the current exporter does not publish that return value. An API's documented
  availability does not prove this aircraft/session exports it.

The current samples contained only the incorrect `rwr_count` field, with no
verified `tws` or `locked` contacts. The RWR display's priority letter is not a
threat list. These generic tables remain `Unverified`, and `contacts` is always an
empty array. World export data is neither inspected by this module nor forwarded.
`world_data_excluded` is always true. No exporter or DCS installation file was
changed for this feature.

## Freshness and session boundaries

The collector remains the only UDP listener. Its backward-compatible
`cockpit_displays` block retains `age_seconds`, `raw`, `survey_ids` and `ufc`, and adds:

```json
{
  "received_epoch_by_id": {"3": 100.0},
  "age_seconds_by_id": {"3": 0.25},
  "display_session": "unique collector display session token",
  "identity_by_id": {
    "3": {
      "aircraft": "FA-18C_hornet",
      "unit_name": "ownship unit",
      "display_session": "same token",
      "model_time_s": 50.0
    }
  }
}
```

An indication packet updates only its own display timestamp. `hello`, `bye`, an
aircraft/unit change or a model-time reset clears all retained display text and
timestamps. The exporter sends a new probe before the first new-slot telemetry:
only a settled matching-aircraft probe received after the previous telemetry,
within one wall-clock second and 0–1 model seconds of the new packet, may survive
that transition. Otherwise the old probe is invalidated. Probe metadata now also
includes `model_time` and `received_epoch`.

Sensor values require all of the following:

- Matching Hornet identity, connected ownship, a verified cockpit, advancing model
  time and ownship/aggregate display ages at most three seconds.
- A settled probe for the same aircraft, with ownship, cockpit and sensor export
  all `AVAILABLE`. Any export denial masks retained labels as well as current ones.
- An individual display receive age at most three seconds, consistent age metadata,
  matching aircraft/unit/display-session identity, and a compatible model timestamp.

The exporter skips empty indications. Therefore a page that stops exporting cannot
immediately clear its old text; the independent three-second age limit removes its
current values even if other displays continue to refresh. Pause, stale telemetry,
denied export and unverified session identity suppress values. A delayed indication
from a previous model-time session is rejected.

Legacy states without any per-display metadata may supply only
`last_observed_fields`, marked `Unverified` and **“Last exported display text —
individual display age unknown.”** These historical labels still require fresh
aggregate telemetry, an advancing model and permitted export. Partial/corrupt
timestamp metadata is not treated as a legacy fallback.

## Snapshot contract

Top-level keys are `aircraft`, `status`, `telemetry_age_s`, `display_age_s`,
`display_freshness`, `layers`, `contacts` and `world_data_excluded`. Layers are
`flir`, `laser`, `radar` and `rwr`. Each has `title`, `status`, `source`, `age_s`,
`aggregate_age_s`, `age_scope`, `fields`, `last_observed_fields` and `notes`.

Current layers use status `Available`; their fields have exact `id`, `label`,
`value`, `display_id`, `element`, `source`, `definition_source`, `age_s` and
`confidence`. A label's source is its exact `list_indication` element. Display IDs
are limited to 2, 3 and 4. Text length and allowed formats are bounded. Field IDs:

- `flir.track_mode`, `flir.zoom`, `flir.zoom_text`, `flir.focus`, `flir.mask_label`,
  `flir.target_range`
- `laser.status_label`, `laser.lst_code`, `laser.ltdr_code`, `laser.lst_label`,
  `laser.ltdr_label`

No actuation or keybinding capability is implied by sensor availability.

## Verification and process ownership

Collector restart/ownership and the one-use warm checkpoint are handled by the
launcher separately; this module does not restart processes.

Tests cover independent ages, stale/frozen/paused states, export denial, missing
timestamps, delayed old packets, lifecycle resets, fresh probe ordering, unknown
aircraft, no raw/world leak, malformed input and exact observed label provenance.

## Complete cockpit text inspector

`backend.cockpit.build_cockpit_snapshot(raw, health, now=None)` provides all string
elements already exported under `cockpit_displays.raw`, including anonymous names,
unknown page labels, multiline text and empty strings. It applies no FLIR field
allowlist and does not truncate valid exported text. This is a text inspector, not
a reconstruction of the DDI/HUD pixels, page geometry, video or graphical symbols.
No display element is interpreted as an action, target identity or system state.

The result has `aircraft`, `status`, `telemetry_age_s`, `display_age_s`,
`text_only: true`, `notes`, and `displays`. Each display has:

```json
{
  "id": "3",
  "label": "Right DDI",
  "status": "Available",
  "age_s": 0.25,
  "source": "list_indication(3) via collector.py",
  "elements": [{"name": "MPD_FLIR_LaserStatus_label", "value": "MASK"}],
  "last_observed_elements": [],
  "notes": []
}
```

For the Hornet the measured display labels are 1 HUD, 2 Left DDI, 3 Right DDI,
4 AMPCD, 5 IFEI, 6 UFC and 7 RWR. Other display IDs and other aircraft use the
generic name `Display N`, because transport metadata does not establish their
cockpit layout. Slots 1–7 remain visible when empty, and additional raw/survey IDs
appear automatically.

Only `Available` displays have current `elements`. `Stale` displays, or those
whose model advancement is unverified (`Unavailable`), may carry values solely
in `last_observed_elements` with an explicit historical note. Those historical
values still require fresh ownship context, matching per-display aircraft/unit/
session identity, valid timestamp metadata, and a settled matching export probe.
Legacy snapshots lacking per-display identity are `Unverified` and show no raw
text; this complete inspector intentionally has no legacy retained-text fallback.

Any denied ownship, cockpit or sensor capability produces `Denied` and hides both
arrays. The sensor denial gate applies to the entire text feed because an arbitrary
cockpit page may contain sensor data. Stale ownship, a disconnected cockpit or
changed aircraft identity also hides retained text. One fresh display never makes
another stale display current. A fresh empty packet is `Available` with zero
elements; no packet is `Unavailable`.

Clients must render element names and values as escaped text, including values
containing HTML-like characters. They must age `Available` values between snapshots
and cease presenting them as current after three seconds. No world-object tables,
raw sensor structures or other unrelated state blocks are read by this inspector.
Malformed nested/non-text elements are omitted with a note; valid exported strings
are preserved exactly. The 43 focused cockpit tests cover these boundaries.
