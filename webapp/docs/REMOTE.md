# Phone and tablet cockpit workspace

Open **Remote** after pairing the device. On a wide browser, **All displays** puts
the Hornet's **Left DDI, AMPCD and Right DDI** in cockpit order, with controls
around each screen. The bezel matches the installed Hornet definitions: left
top-to-bottom 5–1, top left-to-right 6–10, right top-to-bottom 11–15, and bottom
left-to-right 20–16. Button 18 remains bottom centre. Source controls and capture
status sit outside the square screen.

The phone overview stacks the same three displays with their bezel controls.
The glass uses the available phone width, and each button is aligned to the
Hornet menu anchor inside the captured image. Controls fit without overlapping
neighbours; the narrowest layouts slightly shorten targets along each edge.
Individual panels still offer large **Touch** controls or **Cockpit** bezels,
using the same registered action IDs and current bindings. Native Hornet video
and independently aged exported text remain separate source choices.

Individual phone layouts show one selected panel at a time. Wider layouts can show panels
together. Edit the layout to choose individual-panel order, visibility and size; save separate
preferences for each aircraft and phone/tablet/desktop size. Layout import/export
contains only presentation preferences and favourite IDs. It cannot carry Lua,
keyboard commands, credentials or someone else's controller configuration.

The overview is driven by aircraft display definitions rather than a fixed
three-card template. For aircraft without a verified visual layout, it shows
actual exported text displays with their source labels and wraps any display
count; empty unexported placeholder slots are omitted. New aircraft can supply
their own ordering, normalized menu positions and explicit button IDs per edge. Native video still requires
a verified export/capture mapping for that aircraft; a generic text card does not
enable video or fabricate a Hornet bezel for another jet.

The **Controls** reference page is separate from the remote. It starts with search,
device/category selection and favourites. Its optional diagrams filter the list;
they never operate the aircraft. Large catalogues load additional matching rows
on request to keep the phone responsive.

## Connect and operate

Choose **Connect controls** deliberately. Preview is the default: operations are
validated and recorded, but the keyboard sender remains dry run and Lua is not
submitted. A page reload never starts an action or resumes an earlier workflow.

On the paired phone, choose **Connect controls**, then **Enable live controls**.
The phone can enable its own controls; a separate PC control page is not required.
Keep DCS focused on the computer while tapping the phone. The remote's live state
is separate from the older Guide's startup setting and is shown explicitly.

Live enable belongs to the connected screen that enabled it and is tied to the
current aircraft, process, mission/session and binding generation. Leaving that
screen's Remote workspace, losing its heartbeat, reloading or changing the flight
context ends its live enable. Another screen merely connecting, disconnecting or
expiring does not disable the phone. An explicit enable on a different screen
transfers live control to that screen. Reconnect and enable deliberately after a
screen lock or browser suspension; previous actions never resume automatically.

**Disconnect controls** disconnects only that screen. **Stop current run** remains
a shared stop and disarms live operation. Workflow editing stays on the DCS PC.

Hornet support covers resolved momentary UFC, left/right DDI and AMPCD buttons.
The current installed defaults and saved keyboard changes must establish each
button's identity and an unambiguous usable binding. When an action has several
existing bindings, the first supported conflict-free binding is used. An unsafe
alternate does not disable a clean binding for the same button. Buttons without
a usable binding show a reason; this feature does not change DCS assignments. The existing
F-22 benign navigation selections are also available. DCS-BIOS and virtual joystick
drivers are not required for this implementation.

Each touch is a bounded press and release, not a latched keyboard hold. DDI and UFC
button effects depend on the page and mode currently shown in DCS. A delivery audit
establishes a send, not a confirmed cockpit effect. There are no flight-axis or
arbitrary keyboard entry APIs. Remote work and Guide actions share one input channel.

Button requests begin the existing focus check when accepted and release their
busy state as soon as the final press/release receipt arrives. Current telemetry,
process identity and binding file hashes are still checked before each send;
these checks avoid copying the full controls list. The browser also releases its
request latch after acknowledgement without waiting for an extra status request.
Queued or running work still blocks another press, and requests are never retried
automatically. Timing tests use an injected sender and do not measure phone Wi-Fi
or prove a cockpit effect.

## Macros and workflows

On the computer, create a named workflow for its aircraft. Supported steps are:

- A named, supported cockpit action resolved against current bindings.
- A short delay.
- A bounded wait for an exact, independently fresh display field/value.
- A registered mission script from the PC library.

The phone can run saved workflows, inspect progress and choose **Stop**. A run has
one durable identity, and consumes each attempted step before dispatch. A timeout
or uncertain result stops the run instead of repeating an action. A backend restart
cancels pending work and never resumes it automatically. Previewing a wait checks
its definition; it does not claim that a cockpit transition occurred.

Stop prevents future steps and disables remote live operation. An input already
handed to the original sender completes its bounded release. Lua already executing
inside DCS cannot be interrupted or undone by Stop.

## DCS mission scripts

The registered library lives in `webapp/scripts`. The included **Show mission time**
example displays a five-second message inside DCS and presses no cockpit controls.
Each entry pairs a Lua file with a manifest containing the reviewed source hash,
description and allowed aircraft. A changed file becomes unavailable until its
reviewed hash is updated on the PC. The phone submits only the script's identifier.

Scripts use the already installed local mission bridge in the selected Saved Games
profile. They require verified single-player state and current simulation identity.
The dispatch repeats its mode check inside the DCS hooks callback; a mission-side
receipt reports completion without claiming the intended effect was independently
observed. Registered Lua is trusted local code, not a sandbox. Use short finite
routines, and do not expect Stop to undo self-scheduled background work.

See [the script registration guide](../scripts/README.md) for the manifest format.
Legacy mission-specific files under the parent `macros` directory are not imported
automatically, because their bindings and mission assumptions require review.

## Mission scope layers

**Friendlies** and **Enemies** use the same checkbox-style layer controls as
Airfields, Navaids, Route, Mission and Rings. Each independently controls both
its map symbols and its corresponding unit list. These
are live mission-coalition units from the single-player mission bridge, separate
from onboard radar/IFF. Ownship and neutral/dead/inactive units are excluded. Both
layers clear on stale data, session changes, unverified mode or multiplayer. Saved
visibility preferences never save unit positions.

Select a unit on the scope or in the list to populate the existing selection
strip directly below the scope. It uses the same metrics and layout as an airport
selection: bearing, distance from ownship in nautical miles, true heading, ground
speed in knots and altitude in feet MSL. Ground track appears alongside the unit
name when available. There is no separate aircraft details card. Selecting an
airport, navaid or waypoint switches the same strip back to navigation readings.
Distance is recalculated from current ownship and unit positions; hidden or stale
units clear their selection and readings.

Directional markers follow true ground track, with true heading as a fallback.
Track uses horizontal velocity above one knot; heading uses the unit's forward
orientation. The bridge converts local grid vectors to geographic bearings before
labeling them true. North-up and track-up use the same direction data, while names
stay upright. Missing direction produces a neutral symbol instead of a guessed
north-facing aircraft; missing optional values are shown as unavailable.

## Verification boundary

Automated tests and preview runs do not establish successful physical cockpit
operation. Live button effects, a multi-step cockpit workflow, and physical phone
touch ergonomics need pilot observation. The user has confirmed that mobile
display streaming and live bezel buttons work. The latency update was checked
with injected-sender timing tests and read-only checks of the running application;
its actual phone-to-cockpit response still needs pilot observation. Companion
service restarts require pairing again. Pending commands never resume.
