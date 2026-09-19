# Validation boundaries

The source includes automated backend and browser checks. Run them as described in
[DEVELOPMENT.md](DEVELOPMENT.md). Exact local runtime reports, cockpit captures,
authentication records and hardware inventories are deliberately not bundled.

Initial private source package check, 2026-09-19: **1,266 backend tests passed**,
with 19 explicit local-integration skips and one Windows symlink-permission skip;
**161 browser tests passed**. The frontend built from a clean dependency install.
The backend suite used synthetic navigation libraries rather than the original
computer's generated catalogues. GitHub Actions results are recorded separately
by each workflow run.

Scope coverage includes matching layer controls, true heading/track marker
orientation, shared airport/contact details, altitude/speed/range, mobile touch
selection, overlapping touch targets, and revealing the readout above phone
navigation. Display and control suites cover transport, freshness, session
identity, pairing, layout, guards, and reconnect behavior with fixtures.

Live use has been checked on the original Windows/Hornet setup, including actual
moving display content and paired-phone controls. Those observations do not
guarantee compatibility on a new machine or aircraft. The latest scope layout was
also checked against live friendly/enemy data in a touch-emulated browser; its
physical-phone result remains a separate user check.

A new installation should verify:

1. Fresh ownship and cockpit display data with one collector.
2. The selected aircraft's actual bindings and an initial dry-run action.
3. Explicitly enabled benign control response with the pilot observing DCS.
4. Actual changing display imagery on the PC and physical phone simultaneously.
5. Phone refresh/background/reconnect behavior without replaying actions.
6. Single-player Mission Awareness availability only with its compatible bridge.

Never treat increasing image sequence numbers alone as evidence of moving pixels,
or fresh ownship telemetry as evidence that a display export is fresh.
