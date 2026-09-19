"""Read-only input checks and staged refresh for versioned navigation caches.

The caller serializes refresh publishers with its maintenance process lock. No
DCS process, telemetry exporter, input binding or Saved Games file is modified.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from .navigation import NavigationService, SCHEMA, TERRAINS, ROOT, build_terrain_cache, default_community_dir
from ..tools.build_navigation import backed_up_write


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _key(path):
    return str(Path(path).resolve()).replace("\\", "/").casefold()


class NavigationMaintenance:
    def __init__(self, service, dcs_root, cache_dir, runtime_dir):
        self.service = service
        self.dcs_root = Path(dcs_root) if dcs_root else None
        self.cache_dir = Path(cache_dir)
        self.runtime_dir = Path(runtime_dir)

    def _community_dir(self, terrain):
        cache = self.service.caches.get(terrain, {})
        for source in cache.get("provenance", {}).get("sources", []):
            path = Path(source.get("original_source_path", ""))
            if path.name.casefold() in ("airports.py", "projection.py"):
                return path.parent.parent
        return default_community_dir()

    def _inputs(self):
        version_path = self.dcs_root / "autoupdate.cfg"
        version_bytes = version_path.read_bytes()
        config = json.loads(version_bytes.decode("utf-8-sig"))
        version = config.get("version")
        if not isinstance(version, str) or not version or any(c not in "0123456789." for c in version):
            raise ValueError("Invalid installed DCS version")
        folder = self.dcs_root / "Mods/terrains"
        terrain_inputs = {}
        for terrain in TERRAINS:
            directory = folder / terrain
            if not directory.is_dir():
                continue
            terrain_sources = {}
            for name in ("beacons.lua", "radio.lua"):
                matches = [p for p in directory.iterdir() if p.is_file() and p.name.casefold() == name]
                if len(matches) != 1:
                    raise ValueError(f"{terrain}: missing or duplicate {name}")
                terrain_sources[_key(matches[0])] = matches[0]
            for path in (self.dcs_root / "Scripts/World/Radio/BeaconTypes.lua",
                         self.dcs_root / "MissionEditor/modules/Mission/BeaconData.lua"):
                terrain_sources[_key(path)] = path
            subdir = {"Caucasus": "caucasus", "PersianGulf": "persiangulf", "MarianaIslands": "marianaislands"}.get(terrain)
            if subdir:
                for name in ("airports.py", "projection.py"):
                    path = self._community_dir(terrain) / subdir / name
                    # Optional community files are dependencies even when absent,
                    # so additions/deletions change the generation.
                    terrain_sources[_key(path)] = path
            if terrain == "Caucasus":
                path = ROOT / "data/runway-overrides.json"
                terrain_sources[_key(path)] = path
            records = []
            for key, path in sorted(terrain_sources.items()):
                required = path.name.casefold() not in ("airports.py", "projection.py", "runway-overrides.json")
                if not path.exists():
                    if required:
                        raise ValueError(f"Navigation source is missing: {path}")
                    digest = None
                else:
                    if not path.is_file():
                        raise ValueError(f"Navigation source is not a file: {path}")
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                records.append({"path": key, "sha256": digest})
            terrain_inputs[terrain] = {"version": version, "sources": records,
                "fingerprint": _digest({"version": version, "sources": records})}
        if not terrain_inputs:
            raise ValueError("No supported installed terrain source files are available")
        identity = {"version": version, "terrains": {t: v["fingerprint"] for t, v in terrain_inputs.items()}}
        return {**identity, "fingerprint": _digest(identity), "inputs": terrain_inputs,
                "config_hash": hashlib.sha256(version_bytes).hexdigest()}

    @staticmethod
    def _stored_sources(cache):
        sources = {}
        for source in cache.get("provenance", {}).get("sources", []):
            if source.get("original_source_path"):
                sources[_key(source["original_source_path"])] = source.get("source_hash")
        for field in cache.get("airfields", []):
            source = (field.get("measured_centerline") or {}).get("provenance") or {}
            if source.get("original_source_path"):
                sources[_key(source["original_source_path"])] = source.get("source_hash")
        return sources

    def _matches(self, cache, terrain_inputs):
        if not isinstance(cache, dict) or cache.get("schema") != SCHEMA or cache.get("dcs_version") != terrain_inputs["version"]:
            return False
        if cache.get("maintenance", {}).get("input_fingerprint"):
            return cache["maintenance"]["input_fingerprint"] == terrain_inputs["fingerprint"]
        # Accept the caches made before maintenance was installed. Unknown/new
        # source files must trigger one rebuild; source modification times alone
        # do not. Imported runway provenance is deliberately not an input here.
        stored = self._stored_sources(cache)
        for source in terrain_inputs["sources"]:
            if source["sha256"] is not None and stored.get(source["path"]) != source["sha256"]:
                return False
            if source["sha256"] is None and source["path"] in stored:
                return False
        return True

    def _invalid_storage(self, terrains):
        """Missing/corrupt published files also invalidate an in-memory cache."""
        try:
            manifest = json.loads((self.cache_dir / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("schema") != SCHEMA:
                return list(terrains)
            entries = {entry["terrain"]: entry for entry in manifest["terrains"]}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return list(terrains)
        invalid = []
        for terrain in terrains:
            try:
                path = (self.cache_dir / entries[terrain]["path"]).resolve()
                if self.cache_dir.resolve() not in path.parents:
                    raise ValueError("Cache path is outside navigation storage")
                cache = json.loads(path.read_text(encoding="utf-8"))
                if (cache.get("schema") != SCHEMA or cache.get("terrain") != terrain
                        or not isinstance(cache.get("airfields"), list) or not isinstance(cache.get("navaids"), list)
                        or not isinstance(cache.get("provenance", {}).get("coverage"), dict)):
                    raise ValueError("Malformed cache shape")
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                invalid.append(terrain)
        return invalid

    def check(self):
        if self.dcs_root is None:
            return {"valid": False, "status": "Unavailable", "rebuild_eligible": False,
                    "detail": "Choose your DCS installation in Your setup, then restart the companion.",
                    "fingerprint": "unconfigured-installation"}
        try:
            inputs = self._inputs()
            changed = [terrain for terrain, source in inputs["inputs"].items()
                       if not self._matches(self.service.caches.get(terrain), source)]
            invalid_storage = self._invalid_storage(inputs["inputs"])
            changed = sorted(set(changed) | set(invalid_storage))
            removed = sorted(set(self.service.caches) - set(inputs["inputs"]))
            valid = not changed and not removed
            detail = "Installed DCS version and navigation source hashes match" if valid else (
                "Navigation refresh required: " + ", ".join(changed + [f"{t} removed" for t in removed]))
            return {"valid": valid, "status": "Current" if valid else "Invalidated", "detail": detail,
                    "fingerprint": _digest({"inputs": inputs["fingerprint"], "invalid_storage": invalid_storage}) if invalid_storage else inputs["fingerprint"], "dcs_version": inputs["version"],
                    "changed_terrains": changed, "removed_terrains": removed}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Fingerprint the stable reason, not a timestamp; an unchanged error
            # must not defeat the caller's debounce/backoff policy.
            detail = f"Navigation input check unavailable: {exc}"
            return {"valid": False, "status": "Unavailable", "detail": detail,
                    "fingerprint": _digest({"error": detail})}

    def refresh(self):
        """Build changed terrains, recheck all inputs, publish manifest last.

        Raises on incomplete/unstable inputs. The caller keeps navigation masked
        after any exception and retries only after its debounce/backoff policy.
        """
        before = self._inputs()
        invalid_storage = set(self._invalid_storage(before["inputs"]))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        staged = {}
        with tempfile.TemporaryDirectory(prefix="navigation-build-", dir=self.runtime_dir) as staging:
            staging = Path(staging)
            for terrain, source in before["inputs"].items():
                existing = self.service.caches.get(terrain)
                if self._matches(existing, source) and terrain not in invalid_storage:
                    continue
                cache = build_terrain_cache(self.dcs_root, terrain, community_dir=self._community_dir(terrain))
                if cache.get("dcs_version") != before["version"] or cache.get("terrain") != terrain:
                    raise RuntimeError("DCS version/terrain changed during navigation extraction")
                cache["maintenance"] = {"input_fingerprint": source["fingerprint"], "sources": source["sources"]}
                path = staging / f"{terrain}.json"
                path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                # Parse the serialized staging file before touching published data.
                staged[terrain] = json.loads(path.read_text(encoding="utf-8"))
            after = self._inputs()
            if before["fingerprint"] != after["fingerprint"] or before["config_hash"] != after["config_hash"]:
                raise RuntimeError("Navigation sources changed during extraction; nothing published")
            manifest = {"schema": SCHEMA, "terrains": []}
            previous_manifest = self.cache_dir / "manifest.json"
            old_entries = {}
            if previous_manifest.is_file():
                try:
                    old_entries = {e["terrain"]: e for e in json.loads(previous_manifest.read_text(encoding="utf-8"))["terrains"]}
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    pass
            for terrain in before["inputs"]:
                relative = Path(before["version"]) / f"{terrain}.json"
                if terrain in staged:
                    cache = staged[terrain]
                    backed_up_write(self.cache_dir / relative, cache)
                else:
                    cache = self.service.caches[terrain]
                    # Keep an unaffected imported cache byte-for-byte, including
                    # its path. Do not erase runtime geometry on a routine check.
                    if terrain in old_entries:
                        relative = Path(old_entries[terrain]["path"])
                    if not (self.cache_dir / relative).is_file():
                        backed_up_write(self.cache_dir / relative, cache)
                manifest["terrains"].append({"terrain": terrain, "dcs_version": before["version"],
                    "path": relative.as_posix(), "coverage": cache["provenance"]["coverage"]})
            # A fully unchanged refresh should produce no writes or backups.
            try:
                previous = json.loads(previous_manifest.read_text(encoding="utf-8")) if previous_manifest.is_file() else None
            except (OSError, ValueError):
                previous = None
            if previous != manifest:
                backed_up_write(previous_manifest, manifest)
            final_inputs = self._inputs()
            if final_inputs["fingerprint"] != before["fingerprint"] or final_inputs["config_hash"] != before["config_hash"]:
                raise RuntimeError("Navigation sources changed while publishing; keep navigation unavailable until refreshed")
            reloaded = NavigationService(self.cache_dir)
            self.service = reloaded
            return reloaded
