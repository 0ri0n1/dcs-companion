import zipfile

import pytest

from webapp.backend.mission import parse_mission, visible_overlay, MissionError
from webapp.backend.navigation import NavigationService


def miz(tmp_path, text):
    path = tmp_path / "fixture.miz"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mission", text)
        archive.writestr("../../never-extracted.txt", "ignored")
    return path


def test_safe_mission_route_weather_and_coalition(tmp_path, navigation_cache):
    path = miz(tmp_path, '''mission = {
      theatre="Caucasus", weather={wind={atGround={speed=5,dir=90}}},
      coalition={
        blue={country={ [1]={
          plane={group={ [1]={groupId=1,name="Pilot route",
            units={ [1]={unitId=1,name="Fixture Pilot",type="F-22A",skill="Player"}},
            route={points={ [1]={x=-355000,y=617000,alt=1000,name="Home"}, [2]={x=-356000,y=618000,alt=500}}}
          }}},
          static={group={ [1]={groupId=2,units={ [1]={unitId=2,name="Friendly FARP",type="FARP",x=-355000,y=617000}}}}}
        }}},
        red={country={ [1]={
          static={group={ [1]={groupId=3,units={ [1]={unitId=3,name="Hidden FARP",type="FARP",x=-355000,y=617000}}}}}
        }}}
      }
    }''')
    parsed = parse_mission(path, navigation_service=NavigationService(navigation_cache))
    assert parsed["terrain"] == "Caucasus"
    assert len(parsed["routes"][0]["points"]) == 2
    assert parsed["routes"][0]["points"][0]["lat"] is not None
    assert parsed["weather"]["source"] == "DCS mission weather"
    assert parsed["weather"]["surface_wind"]["from_true_deg"] is None
    visible = visible_overlay(parsed, {"aircraft": {"unit_name": "Fixture Pilot", "aircraft": "F-22A"}})
    assert len(visible["route"]) == 2
    assert [o["name"] for o in visible["objects"]] == ["Friendly FARP"]
    assert visible_overlay(parsed, {"aircraft": {"aircraft": "F-15C"}})["objects"] == []
    assert not (tmp_path.parent / "never-extracted.txt").exists()


@pytest.mark.parametrize("text", ['mission=os.execute("bad")', 'mission={theatre="Caucasus"}; dofile("bad")',
                                  'mission={theatre="Syria"}', 'mission={theatre="Caucasus",theatre="PersianGulf"}',
                                  'mission={theatre="Caucasus",coalition=10}',
                                  'mission={theatre="Caucasus",weather={wind=10}}'])
def test_mission_rejects_expressions_unknown_theatre_and_duplicate_keys(tmp_path, text):
    with pytest.raises(MissionError):
        parse_mission(miz(tmp_path, text))


def test_no_omniscient_unit_export(tmp_path):
    parsed = parse_mission(miz(tmp_path, '''mission={theatre="Caucasus",coalition={red={country={{
       vehicle={group={{groupId=2,units={{unitId=2,name="Enemy tank",type="T-72B",x=0,y=0}}}}},
       plane={group={{groupId=3,units={{unitId=3,name="Enemy fighter",type="Su-27",x=0,y=0}}}}}
    }}}}}'''))
    assert parsed["objects"] == []


def test_archive_size_and_duplicate_member_limits(tmp_path, monkeypatch):
    path = miz(tmp_path, 'mission={theatre="Caucasus"}')
    monkeypatch.setattr("webapp.backend.mission.MAX_MISSION_BYTES", 2)
    with pytest.raises(MissionError):
        parse_mission(path)
