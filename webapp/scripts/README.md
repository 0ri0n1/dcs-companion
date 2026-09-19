# Registered DCS mission scripts

This folder is a library of trusted scripts managed on the DCS computer. The phone
can run a registered identifier; it cannot upload Lua or select a filesystem path.
Scripts run in the current **single-player mission** through the existing local
DCS Fiddle bridge. They are not cockpit-export scripts, Windows programs or shell
commands. The bridge must already be available in the selected Saved Games profile.

Each script uses two files with the same identifier:

- `mission-status.lua`: UTF-8 Lua, maximum 16 KiB.
- `mission-status.json`: description, allowed aircraft and reviewed SHA-256.

The included **Show mission time** example displays a short message in DCS. It
does not press cockpit controls. Preview mode does not submit any Lua to DCS.

To register your own script, choose a lowercase identifier starting with a letter
(letters, digits, hyphen and underscore; at most 64 characters). Place the Lua file
here, inspect its contents, then create its JSON manifest using this shape:

```json
{
  "schema": "dcs-companion/mission-script/1",
  "id": "your-script",
  "name": "Your script",
  "description": "Explain its effects before someone runs it.",
  "context": "mission",
  "aircraft": ["FA-18C_hornet"],
  "sha256": "the lowercase SHA-256 of the exact Lua file bytes",
  "enabled": true
}
```

`Get-FileHash -Algorithm SHA256 -LiteralPath .\your-script.lua` computes the hash
in PowerShell. Use `["*"]` only for a script intended for every aircraft. Changing
the Lua invalidates its registration until its reviewed hash is updated on the PC.
The companion lists at most 64 registered manifests. It does not import the user's
older mission-specific macro files automatically.

Remote control must be connected and live operation explicitly enabled on the PC.
The runner checks aircraft, session, source revision and advancing simulation. The
single-player check is repeated inside the DCS hooks callback before dispatch.
An execution receipt records whether Lua returned successfully; it does not prove
its intended cockpit or mission effect occurred. An uncertain result is never
retried automatically.

**Stop cancels unsent work and later workflow steps. It cannot interrupt Lua already
executing inside DCS or undo its effects.** Scripts are trusted code, not sandboxed:
use short, finite routines. Avoid unbounded loops and self-scheduled background work
if you need Stop to control the whole workflow. Disable a script with `enabled:false`.

Keep personal scripts and generated mission data out of shared layout exports.
