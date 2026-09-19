"""Thin adapter around the original guarded scan-code sender."""
from pathlib import Path
import json
import os


class GuardedSender:
    def __init__(self, allowlist_path, audit_path, dry_run=True, ensure_release=False):
        self.allowlist_path = Path(allowlist_path)
        self.audit_path = Path(audit_path)
        self.dry_run = dry_run
        self.instance = None
        self.index_hash = None
        self.ensure_release = ensure_release

    def __call__(self, action, request):
        from input_sender import KeySender
        import hashlib
        content = self.allowlist_path.read_bytes()
        index = json.loads(content)
        permitted = [a for a in index.get("actions", []) if a.get("name") == action["name"]]
        if len(permitted) != 1 or permitted[0].get("combos", []) != [action["combo"]]:
            return {"ok": False, "sent": False, "message": "Sender allowlist does not match the selected action."}
        kind, _ = KeySender.classify(action["name"])
        if kind != "ok":
            return {"ok": False, "sent": False, "message": "Existing sender refused the action classification."}
        digest = hashlib.sha256(content).hexdigest()
        if self.instance is None or self.index_hash != digest:
            previous_send = self.instance._last_send if self.instance else 0
            sender_type = _releasing_sender(KeySender) if self.ensure_release else KeySender
            self.instance = sender_type(str(self.allowlist_path), dry_run=self.dry_run, audit=str(self.audit_path))
            self.instance._last_send = previous_send
            self.index_hash = digest
        loaded = self.instance.binds
        loaded_action = self.instance.actions.get(action["name"], {})
        if loaded_action.get("combos") != [action["combo"]] or loaded.get("by_combo", {}).get(action["combo"]) != [action["name"]]:
            return {"ok": False, "sent": False, "message": "Loaded sender index changed during validation."}
        if action.get("bindings_fingerprint") and (loaded.get("aircraft") != action.get("aircraft") or loaded.get("source_fingerprint") != action["bindings_fingerprint"]):
            return {"ok": False, "sent": False, "message": "Sender generation or aircraft does not match the current binding snapshot."}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        # Fail before SendInput if the audit destination is unwritable.
        with self.audit_path.open("ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())
            offset = handle.tell()
        ok, message = self.instance.send_action(action["name"], force=False, require_focus=True)
        with self.audit_path.open("rb") as handle:
            handle.seek(offset)
            audit_text = handle.read(32768).decode("utf-8", errors="replace")
        expected = ("DRY-RUN combo=" if self.dry_run else "SENT combo=") + action["combo"]
        evidence = [line for line in audit_text.splitlines() if expected in line]
        sent = bool(ok and not self.dry_run and len(evidence) == 1 and "SEND ERROR" not in audit_text)
        return {"ok": bool(ok and len(evidence) == 1), "sent": sent, "dry_run": self.dry_run,
                "ambiguous": not self.dry_run and not sent and (ok or "SEND ERROR" in audit_text),
                "message": message if evidence else "Sender audit evidence missing; never retry automatically.",
                "audit_path": str(self.audit_path), "audit_offset": offset, "audit_evidence": evidence,
                "request_id": request["request_id"]}


def _releasing_sender(base):
    """Use the original emitter, with best-effort release on partial failure.

    This does not claim the OS accepted a failed release. Every failure remains
    ambiguous and is never retried as a new press. Original protected file stays
    unchanged; guide callers retain their existing sender behavior.
    """
    class ReleasingKeySender(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.held = []

        def _emit(self, scancode, extended, keyup):
            key = (scancode, extended)
            if not keyup and key not in self.held:
                self.held.append(key)
            result = super()._emit(scancode, extended, keyup)
            if keyup and key in self.held:
                self.held.remove(key)
            return result

        def send_action(self, *args, **kwargs):
            try:
                return super().send_action(*args, **kwargs)
            finally:
                for scancode, extended in reversed(self.held[:]):
                    try:
                        self._emit(scancode, extended, True)
                    except Exception:
                        self.log('SEND ERROR cleanup key release could not be verified')
    return ReleasingKeySender
