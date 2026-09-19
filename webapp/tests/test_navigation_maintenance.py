import json
from pathlib import Path

import pytest

from webapp.backend.navigation import NavigationService
from webapp.backend.navigation_maintenance import NavigationMaintenance
import webapp.backend.navigation_maintenance as maintenance_module


def create_terrain(install, name, callsign="Field"):
    folder = install / "Mods/terrains" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Beacons.lua").write_text("beacons={}")
    (folder / "Radio.lua").write_text("radio={{radioId='airfield1_0',callsign={ {[\"common\"]={_('" + callsign + "'),'" + callsign + "'}} }, role={'tower'},frequency={[VHF_HI]={MODULATIONTYPE_AM,125000000}}}}")


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    install = tmp_path / "DCS"
    install.mkdir()
    (install / "autoupdate.cfg").write_text('{"version":"1.2.3"}')
    for rel in ("Scripts/World/Radio/BeaconTypes.lua", "MissionEditor/modules/Mission/BeaconData.lua"):
        path = install / rel
        path.parent.mkdir(parents=True)
        path.write_text("-- fixture source")
    create_terrain(install, "Caucasus")
    create_terrain(install, "PersianGulf")
    monkeypatch.setattr(maintenance_module, "ROOT", tmp_path)
    monkeypatch.setattr(NavigationMaintenance, "_community_dir", lambda self, terrain: tmp_path / "community")
    cache = tmp_path / "cache"
    runtime = tmp_path / "runtime"
    helper = NavigationMaintenance(NavigationService(cache), install, cache, runtime)
    return helper, install, cache, runtime


def test_build_missing_and_unchanged_check_does_not_write(fixture):
    helper, install, cache, runtime = fixture
    assert helper.check()["valid"] is False
    service = helper.refresh()
    assert set(service.caches) == {"Caucasus", "PersianGulf"}
    assert helper.check()["valid"] is True
    fingerprint = helper.check()["fingerprint"]
    before = {p.relative_to(cache): (p.read_bytes(), p.stat().st_mtime_ns) for p in cache.rglob("*") if p.is_file()}
    helper.refresh()
    assert helper.check()["fingerprint"] == fingerprint
    after = {p.relative_to(cache): (p.read_bytes(), p.stat().st_mtime_ns) for p in cache.rglob("*") if p.is_file()}
    assert before == after
    assert not list(runtime.glob("navigation-build-*"))


def test_same_version_only_changed_terrain_rebuilds_preserves_geometry(fixture):
    helper, install, cache, runtime = fixture
    service = helper.refresh()
    field = service.caches["Caucasus"]["airfields"][0]
    field["runways"] = [{"id": "trusted-api-fixture", "length_m": 2000}]
    field["runway_geometry_status"] = "Terrain API geometry"
    saved = cache / "1.2.3/Caucasus.json"
    saved.write_text(json.dumps(service.caches["Caucasus"]))
    unchanged_bytes = saved.read_bytes()
    create_terrain(install, "PersianGulf", callsign="Changed Field")
    check = helper.check()
    assert check["changed_terrains"] == ["PersianGulf"]
    rebuilt = helper.refresh()
    assert saved.read_bytes() == unchanged_bytes
    assert rebuilt.caches["Caucasus"]["airfields"][0]["runways"][0]["id"] == "trusted-api-fixture"
    assert rebuilt.caches["PersianGulf"]["airfields"][0]["name"] == "Changed Field"
    assert list((cache / "1.2.3").glob("PersianGulf.json.bak-*"))
    assert not list((cache / "1.2.3").glob("Caucasus.json.bak-*"))


def test_version_change_rebuilds_all_and_keeps_old_generation(fixture):
    helper, install, cache, runtime = fixture
    helper.refresh()
    old_files = {p.name: p.read_bytes() for p in (cache / "1.2.3").glob("*.json")}
    (install / "autoupdate.cfg").write_text('{"version":"1.2.4"}')
    assert set(helper.check()["changed_terrains"]) == {"Caucasus", "PersianGulf"}
    service = helper.refresh()
    assert {c["dcs_version"] for c in service.caches.values()} == {"1.2.4"}
    assert old_files == {p.name: p.read_bytes() for p in (cache / "1.2.3").glob("*.json")}
    assert list(cache.glob("manifest.json.bak-*"))


def test_add_delete_terrain_and_source_files(fixture):
    helper, install, cache, runtime = fixture
    helper.refresh()
    create_terrain(install, "MarianasWWII")
    assert helper.check()["changed_terrains"] == ["MarianasWWII"]
    helper.refresh()
    radio = install / "Mods/terrains/MarianasWWII/Radio.lua"
    original = radio.read_bytes()
    radio.unlink()
    assert helper.check()["status"] == "Unavailable"
    with pytest.raises(ValueError):
        helper.refresh()
    radio.write_bytes(original)
    folder = install / "Mods/terrains/MarianasWWII"
    folder.rename(install / "Mods/terrains/disabled-fixture")
    assert helper.check()["removed_terrains"] == ["MarianasWWII"]
    assert set(helper.refresh().caches) == {"Caucasus", "PersianGulf"}


def test_optional_source_addition_and_deletion_detected(fixture):
    helper, install, cache, runtime = fixture
    helper.refresh()
    community = helper._community_dir("Caucasus") / "caucasus"
    community.mkdir(parents=True)
    # An incomplete optional pair still changes the input signature; never adopt
    # coordinates without both supported data files.
    path = community / "airports.py"
    path.write_text("# no airport data")
    assert helper.check()["changed_terrains"] == ["Caucasus"]
    helper.refresh()
    assert helper.check()["valid"] is True
    path.unlink()
    assert helper.check()["changed_terrains"] == ["Caucasus"]


def test_source_changes_mid_build_publish_nothing(fixture, monkeypatch):
    helper, install, cache, runtime = fixture
    helper.refresh()
    old_files = {str(p): p.read_bytes() for p in cache.rglob("*") if p.is_file()}
    create_terrain(install, "PersianGulf", callsign="Changed")
    real_builder = maintenance_module.build_terrain_cache
    def unstable_builder(*args, **kwargs):
        result = real_builder(*args, **kwargs)
        (install / "Mods/terrains/PersianGulf/Radio.lua").write_text("radio={}")
        return result
    monkeypatch.setattr(maintenance_module, "build_terrain_cache", unstable_builder)
    with pytest.raises(RuntimeError, match="changed during extraction"):
        helper.refresh()
    assert old_files == {str(p): p.read_bytes() for p in cache.rglob("*") if p.is_file()}
    assert helper.check()["valid"] is False


def test_bad_version_and_malformed_source_fail_closed(fixture):
    helper, install, cache, runtime = fixture
    (install / "autoupdate.cfg").write_text("not json")
    first = helper.check()
    assert first["valid"] is False and first["status"] == "Unavailable"
    assert helper.check()["fingerprint"] == first["fingerprint"]
    with pytest.raises(ValueError):
        helper.refresh()
    (install / "autoupdate.cfg").write_text('{"version":"1.2.3"}')
    (install / "Mods/terrains/Caucasus/Radio.lua").write_text("radio=os.execute('bad')")
    with pytest.raises(ValueError):
        helper.refresh()
    assert not (cache / "manifest.json").exists()


def test_manifest_is_published_last(fixture, monkeypatch):
    helper, install, cache, runtime = fixture
    calls = []
    real_write = maintenance_module.backed_up_write
    def capture(path, value):
        calls.append(Path(path).name)
        return real_write(path, value)
    monkeypatch.setattr(maintenance_module, "backed_up_write", capture)
    helper.refresh()
    assert calls[-1] == "manifest.json"
    assert set(calls[:-1]) == {"Caucasus.json", "PersianGulf.json"}


def test_corrupt_and_missing_cache_recovered_with_backup(fixture):
    helper, install, cache, runtime = fixture
    helper.refresh()
    destination = cache / "1.2.3/Caucasus.json"
    destination.write_text("corrupt cache")
    assert helper.check()["valid"] is False
    helper.refresh()
    assert helper.check()["valid"] is True
    assert any(p.read_text() == "corrupt cache" for p in (cache / "1.2.3").glob("Caucasus.json.bak-*"))
    destination.unlink()
    assert helper.check()["valid"] is False
    helper.refresh()
    assert helper.check()["valid"] is True
    (cache / "manifest.json").write_text("corrupt manifest")
    assert helper.check()["valid"] is False
    helper.refresh()
    assert helper.check()["valid"] is True
