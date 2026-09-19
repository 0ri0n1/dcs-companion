# Portable DCS setup

The companion learns the DCS installation and Saved Games profile on its host instead of assuming the original author's drive, username or controller GUIDs. Discovery is read-only and bounded. It never executes a discovered diff file or changes DCS configuration. Existing binding resolution separately reads installed/default Lua through the established extractor.

## Selection

Discovery checks validated running `DCS.exe` paths (`bin` or `bin-mt` with `Config/Input`), Eagle Dynamics and uninstall registry entries, Steam libraries/manifests and conventional installation folders. Saved Games uses the Windows Known Folder location, including redirected folders, with a home-folder fallback. Running `-w`, installation `dcs_variant.txt` and the normal DCS profile name identify a running profile. Unresolved or competing evidence stays unresolved.

Explicit launch paths or a saved companion selection take precedence. An invalid explicit path does not fall back. `runtime/setup-selection.json` stores only a schema number and the chosen installation/profile paths. Browser selections use current discovered IDs, are revalidated before save, require a paired session and are writable only from loopback on the DCS computer. No endpoint accepts arbitrary file paths, Lua or shell scripts.

The running backend keeps its original active roots. Saving a different selection shows **pending restart**; restarting the companion applies it. Nothing requests a DCS restart. Launch-option overrides lock browser selection until those options are removed. A damaged selection file leaves setup unavailable and can be repaired by selecting detected candidates again. Custom paths outside discovery can be supplied via `--install-root` and `--saved-games`, or the corresponding `DCS_DASH_INSTALL_ROOT` and `DCS_DASH_SAVED_GAMES` variables for direct backend startup. Launcher startup deliberately ignores inherited `DCS_DASH_*` variables and uses its explicit options.

## Devices and bindings

The inventory lists installed module input folders, aircraft Saved Games input folders and keyboard/mouse/joystick diff filenames. Device IDs preserve the DCS GUID where one exists; Keyboard and Mouse are named input kinds. Inventory rows are evidence of saved configuration, not Windows connection enumeration. Counts exclude unrelated global input layers from aircraft counts. Missing or truncated inventories carry issues rather than implying completeness.

F-22A and FA-18C Hornet retain their independent supported catalogues, default-plus-user assignment view and conflict checks. Other discovered aircraft remain inventory only. Binding caches and retained device history must belong to the same installation, profile and module. A foreign cache is withheld. Multiple saved filenames referring to the same joystick GUID are marked ambiguous and their assignments withheld. Missing aircraft modules are unavailable and are not repeatedly rebuilt; installing one permits automatic refresh on the next check.

The current parser has limits: localized names and duplicate display names are not a complete engine command-hash identity system; device-template fallback, custom modifier behavior and generic module loading need further adapter work. Do not claim universal automatic assignment or use a discovered module as authorization to execute its commands. The existing three explicit F-22 navigation actions remain the only browser command candidates. Hornet is a reference catalogue. The existing exporter/collector must still be installed and available for live telemetry.

## Sharing

Setup selection, generated binding/navigation caches, adapter output, verification captures and test output are ignored by the repository rules. They contain local paths, controller identities or flight data and are not portable source assets. This repository was prepared from a reviewed source allowlist. Ignore rules do not remove already tracked material, so inspect each staged change before publishing. See [release readiness](RELEASE-READINESS.md) for the remaining public-release work.

## Verification

Tests exercise alternate installations, Steam metadata, relocated Saved Games, running profile arguments, ambiguous/missing paths, profile-specific cache isolation, duplicate GUID evidence, local-only selection, first run without DCS and responsive setup UI. Live read-only discovery identifies this host's current installation/profile. No binding writes, controller remapping or cockpit commands are part of verification. Different physical PCs and controller models still need real-machine release testing.
