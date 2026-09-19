# Current mission metadata

`backend/live_mission.py` supplies `{status, name, theatre, source, age_s, detail}` through `MissionReader.snapshot(health, raw)`. The call returns immediately. A background worker refreshes at most once every two seconds, independently from command processing; results older than six seconds are withheld.

The reader requires a running DCS process, a current exporter session and an aircraft identity. Menus, process changes and exporter-session changes clear metadata immediately. An outstanding read from an earlier identity cannot publish into the new one. A paused mission can retain freshly queried metadata even when ownship telemetry stops advancing. `DCS.getPause()` supplies an explicit paused label when available.

If the collector restarts while DCS is already paused, its restored checkpoint is deliberately disconnected until a real packet arrives. During that interval the application cannot distinguish a paused cockpit from menus with retained old values, so mission metadata waits for the first real exporter packet. The installed mission-side bridge runs on a simulation timer and cannot provide a fresh mission-environment response while paused. Historical log or checkpoint evidence does not override the menu/disconnected gate.

Source order:

1. Explicit mission metadata in the current fresh collector session, when provided.
2. The already installed DCS Fiddle bridge on loopback. Fixed queries read only `DCS.getMissionName()`, `DCS.getModelTime()`, `DCS.getPause()`, `env.mission.sortie`, `env.mission.theatre` and `timer.getTime()`. No browser parameter can supply Lua code, a host, port or path. The hooks and mission model times must agree within three seconds before their metadata is combined.
3. Exact current-process DCS log lifecycle: `Dispatcher: loadMission ...miz`, optional `Terrain theatre ...`, then `loadMission Done: Control passed to the player`. A subsequent load, stop or menu transition clears the previous mission. This fallback explicitly labels its name as the loaded filename, not a resolved briefing title. An old cached mission filename, editor history or arbitrary saved `.miz` file is never promoted into an active mission.

The installed Fiddle credentials are read locally from its existing hook configuration, used only for authenticated requests to fixed loopback ports 12080/12081, and never included in responses or diagnostic error text. HTTP redirects and environment proxy settings are not used. Each bridge socket has a 0.4-second timeout and a 16 KiB response limit. Log reading is incremental and bounded to 4 MiB per refresh. No hook, exporter, DCS installation file, Saved Games profile or mission archive is modified.

Mission title/theatre display does not establish an active navigation-overlay hash and does not change the command queue's session identity. No world units, coalition objects, enemy positions or mission-object lists are queried.

Local verification on September 18, 2026: the installed 8898 export bridge provides model/start time and ownship type, but no title/theatre; no 8899 hook listener was present. The existing Fiddle bridge returned DCS mission name `tempMission` and theatre `PersianGulf`. Its live sortie field was `DictKey_sortie_5`; a one-time bounded inspection of only the dictionary member of the exact log-loaded temporary mission showed that key's authored title was empty. The dashboard therefore displays the actual DCS name without inventing a fuller title. Production polling does not read mission archives.

Nineteen isolated tests cover complete and incomplete log lifecycles, process fences, loading/menu/stop invalidation, log rotation, nonblocking refresh, in-flight session replacement, stale data masking, paused ownship, fixed query scope, dictionary-key rejection, bridge failures and mixed-generation metadata.
