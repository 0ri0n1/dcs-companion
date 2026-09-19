import json
from webapp.backend.sender import GuardedSender


def test_original_sender_dry_run_appends_exactly_one_audit_and_never_emits(tmp_path, monkeypatch):
    import input_sender
    def forbidden_emit(*args, **kwargs):
        raise AssertionError("Dry run must never send input")
    monkeypatch.setattr(input_sender.KeySender, "_emit", forbidden_emit)
    index = tmp_path / "binds.json"
    index.write_text(json.dumps({"actions": [{"name": "Next Waypoint, Airfield Or Target", "combos": ["LCtrl+`"]}], "by_combo": {"LCtrl+`": ["Next Waypoint, Airfield Or Target"]}}))
    audit = tmp_path / "audit.log"
    sender = GuardedSender(index, audit, dry_run=True)
    result = sender({"name": "Next Waypoint, Airfield Or Target", "combo": "LCtrl+`"}, {"request_id": "test"})
    assert result["ok"] and result["dry_run"] and not result["sent"]
    assert len(audit.read_text().splitlines()) == 1
    assert "DRY-RUN" in audit.read_text()
