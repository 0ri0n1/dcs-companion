# Appearance and readability

Open **Appearance** in the site navigation to choose a color theme and text size. These are display settings; changing them sends no aircraft input.

## Color themes

| Choice | Appearance |
| --- | --- |
| Automatic | Follows the aircraft reported by fresh telemetry. |
| Classic | The familiar dark green companion style. |
| Hornet | A green cockpit-inspired palette for the F/A-18C. |
| Raptor | A cool palette for the F-22 and F-23. |
| Eagle | An amber-accented palette for F-15 variants. |
| Lightning | A violet-accented palette for F-35 variants. |
| Daylight | A light background with dark text for brighter rooms. |
| High contrast | A dark background with strong text separation. |

All themes remain available manually for any aircraft. They change the companion's appearance only; choosing a theme does not add aircraft support or change a control catalogue.

Automatic mode retains the last recognized aircraft palette while DCS is in menus, telemetry is stale, or the browser connection is interrupted. A new page with no recognized live aircraft starts with High contrast. Selecting a named theme keeps that choice through aircraft changes until Automatic is selected again.

## Text size

**Comfortable** uses clearer labels and secondary text throughout the site. **Large** increases these further for a more distant second monitor or tablet. The page can scroll vertically as needed; the scope, panels and controls remain available.

The existing brightness control still applies to the site. Use full brightness when the priority is maximum readability, then reduce it to suit the room.

**Reset appearance** restores Automatic, Comfortable and full brightness. Close the panel with **Done**, the close button or Escape; keyboard focus returns to Appearance.

## What is saved

The browser saves only the selected `theme` and `textSize` alongside the existing display preferences in `dcs-copilot-display-v1`. The current aircraft and the automatically resolved palette are not persisted. No telemetry, enemy contacts, commands or pairing credentials are added to browser storage. Corrupt or unsupported preference values fall back to Automatic and Comfortable.

## Verification

`frontend/tests/themes.spec.ts` uses explicitly synthetic browser fixtures to check aircraft matching, manual override, reload persistence, malformed preference recovery, absence of command submissions, rendered text contrast, and horizontal layout at 2560×1440, 1180×820 and 820×1180. Readability checks exercise all seven main views using Daylight and High contrast with Large text. These browser checks do not constitute aircraft or flight validation.

The focused suite passed all 17 cases on 2026-09-18. Sampled ordinary text and scope labels in every palette met a 4.5:1 contrast threshold at full brightness; this is a targeted regression check, not a complete accessibility certification. Selected screenshots are written to `frontend/test-results/themes/`.
