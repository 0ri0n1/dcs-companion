"""Durable one-slot queue; an ambiguous attempt is never retried."""
import json
import math
import re
import sqlite3
import threading
import time
from uuid import UUID

PRE_SEND = {"Selected", "Queued", "Awaiting DCS Focus", "Settling", "Revalidating"}
TERMINAL = {"Confirmed", "Unconfirmed", "Rejected", "Expired", "Cancelled"}
IDENTITY = ("aircraft", "session", "mission", "process", "bindings_hash")
FORBIDDEN = re.compile(r"pitch|roll|yaw|rudder|aileron|elevator|view|throttle|weapon|gun|fire|jettison|eject|engine|canopy|emergency|master arm", re.I)


class QueueError(ValueError):
    pass


class CommandQueue:
    def __init__(self, db_path, context, actions, sender, *, dry_run=True, expiry_s=15,
                 settle_s=.8, clock=time.time, confirmer=None):
        self.context_provider, self.actions_provider, self.sender = context, actions, sender
        self.dry_run, self.expiry_s, self.settle_s = dry_run, expiry_s, settle_s
        self.clock, self.confirmer = clock, confirmer
        self.stopped = False
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, payload TEXT NOT NULL, active INTEGER NOT NULL, attempted INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_slot ON commands(active) WHERE active=1")
        for row in self.db.execute("SELECT * FROM commands WHERE active=1").fetchall():
            item = json.loads(row["payload"])
            self._transition(item, "Unconfirmed" if row["attempted"] else "Cancelled",
                             "Backend restarted; request will never be replayed.")
        self.db.commit()

    def _save(self, item, active=None):
        active = item["state"] not in TERMINAL if active is None else active
        self.db.execute("UPDATE commands SET payload=?,active=? WHERE id=?",
                        (json.dumps(item, allow_nan=False), int(active), item["request_id"]))
        self.db.commit()

    def _transition(self, item, state, detail):
        item.update(state=state, detail=detail, updated_at=self.clock())
        item["history"].append({"state": state, "at": self.clock(), "detail": detail})
        self._save(item)

    def get(self, request_id=None):
        with self.lock:
            row = self.db.execute("SELECT payload FROM commands WHERE id=?", (request_id,)).fetchone() if request_id else self.db.execute("SELECT payload FROM commands ORDER BY rowid DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else {"state": "Idle", "history": [], "dry_run": self.dry_run}

    @staticmethod
    def validation(action, ctx):
        if action.get("risk") != "benign" or FORBIDDEN.search(action.get("name", "")):
            return "This action is display-only and cannot execute through focus return."
        if not action.get("combo") or not action.get("allowlisted", True) or action.get("conflicts"):
            return "Action is unbound, conflicting, or outside the current allowlist."
        if not ctx.get("process") or not ctx.get("aircraft") or not ctx.get("session") or not ctx.get("mission"):
            return "DCS process, aircraft and session identity are required."
        if not ctx.get("telemetry_valid") or not ctx.get("model_advancing"):
            return "Fresh advancing cockpit telemetry is required."
        model_time = ctx.get("model_time")
        if not isinstance(model_time, (int, float)) or not math.isfinite(model_time):
            return "Current aircraft model time is required."
        if action.get("aircraft", "F-22A") != ctx.get("aircraft"):
            return "Wrong aircraft for this action."
        if ctx.get("setup_matches_process") is False:
            return "The selected DCS setup does not match the running installation and profile. Open Your setup."
        if not ctx.get("bindings_valid") or not ctx.get("bindings_hash"):
            return "Binding index is invalid or changed. Rebuild before queuing."
        if action.get("requires_display") and not ctx.get("display_valid"):
            return "Fresh cockpit displays are required for this action."
        return None

    def enqueue(self, request_id, action_id, confirmed=False):
        with self.lock:
            if self.stopped:
                raise QueueError("Dashboard is stopping; no new commands are accepted.")
            try:
                if str(UUID(request_id)) != request_id.lower():
                    raise ValueError()
                request_id = str(UUID(request_id))
            except (ValueError, TypeError, AttributeError):
                raise QueueError("A unique UUID request_id is required.")
            prior = self.db.execute("SELECT payload FROM commands WHERE id=?", (request_id,)).fetchone()
            if prior:
                result = json.loads(prior[0])
                if result["action_id"] != action_id:
                    raise QueueError("This request ID is already associated with another action.")
                return result
            if not confirmed:
                raise QueueError("Deliberately confirm the selected action before queuing; DCS may already be focused.")
            if self.db.execute("SELECT 1 FROM commands WHERE active=1").fetchone():
                raise QueueError("One action is already queued. Cancel it first.")
            action = next((a for a in self.actions_provider() if a["id"] == action_id), None)
            ctx = self.context_provider()
            now = self.clock()
            item = {"request_id": request_id, "action_id": action_id, "state": "Selected", "dry_run": self.dry_run,
                    "created_at": now, "expires_at": now + self.expiry_s, "history": [], "context": ctx,
                    "action": action, "detail": "Action selected.", "sent": False}
            try:
                self.db.execute("INSERT INTO commands(id,payload,active) VALUES(?,?,1)", (request_id, json.dumps(item)))
                self.db.commit()
            except sqlite3.IntegrityError as exc:
                self.db.rollback()
                raise QueueError("Another request already holds the queue slot.") from exc
            self._transition(item, "Selected", "Action selected; explicit confirmation received.")
            reason = self.validation(action, ctx) if action else "Action is unsupported or not in the benign F-22 allowlist."
            if reason:
                self._transition(item, "Rejected", reason)
            else:
                self._transition(item, "Queued", "One action queued. Return to DCS; cancel remains available.")
            return item

    def cancel(self, request_id):
        with self.lock:
            try:
                request_id = str(UUID(request_id))
            except (ValueError, TypeError, AttributeError):
                raise QueueError("Invalid request ID.")
            item = self.get(request_id)
            if item.get("request_id") != request_id:
                raise QueueError("Unknown request.")
            if item["state"] in PRE_SEND:
                self._transition(item, "Cancelled", "Cancelled before send.")
            return item

    def invalidate(self, reason):
        """Background data maintenance cancels pending work; it never resumes it."""
        with self.lock:
            row = self.db.execute("SELECT payload FROM commands WHERE active=1").fetchone()
            if row:
                item = json.loads(row[0])
                if item["state"] in PRE_SEND:
                    self._transition(item, "Cancelled", reason)
                elif item["state"] in {"Sent", "Confirming"}:
                    self._transition(item, "Unconfirmed", "Observation invalidated after send. " + reason)

    def _mismatch(self, original, current):
        return next((key for key in IDENTITY if original.get(key) != current.get(key)), None)

    def tick(self):
        with self.lock:
            if self.stopped:
                return
            row = self.db.execute("SELECT payload FROM commands WHERE active=1").fetchone()
            if not row:
                return
            item = json.loads(row[0])
            ctx = self.context_provider()
            if item["state"] in {"Sent", "Confirming"}:
                changed = self._mismatch(item["context"], ctx)
                fresh_sample = ctx.get("sample_epoch", 0) and ctx["sample_epoch"] > item.get("sent_sample_epoch", float("inf"))
                if changed or not ctx.get("telemetry_valid"):
                    self._transition(item, "Unconfirmed", "Sent; observation invalidated by session or telemetry change.")
                elif self.confirmer and fresh_sample and self.confirmer(item, ctx):
                    self._transition(item, "Confirmed", "Fresh aircraft-specific transition observed; causation is not attributed.")
                elif self.clock() >= item["confirm_until"]:
                    self._transition(item, "Unconfirmed", "Sent—state not observable" if not item["action"].get("observable") else "Sent; expected transition not confirmed.")
                return
            if self.clock() >= item["expires_at"]:
                self._transition(item, "Expired", "Expired before send; no retry.")
                return
            changed = self._mismatch(item["context"], ctx)
            if changed:
                self._transition(item, "Cancelled", f"Queue invalidated: {changed} changed.")
                return
            actions = {a["id"]: a for a in self.actions_provider()}
            action = actions.get(item["action_id"])
            reason = self.validation(action, ctx) if action else "Action binding is no longer allowlisted."
            if reason:
                self._transition(item, "Rejected", reason)
                return
            if not ctx.get("focused"):
                item.pop("settle_until", None)
                if item["state"] != "Awaiting DCS Focus":
                    self._transition(item, "Awaiting DCS Focus", "Waiting for DCS foreground focus.")
                return
            if "settle_until" not in item:
                item["settle_until"] = self.clock() + self.settle_s
                item["settle_model_time"] = ctx["model_time"]
                self._transition(item, "Settling", "DCS focused; waiting for focus to settle.")
                return
            if self.clock() < item["settle_until"]:
                return
            self._transition(item, "Revalidating", "Checking focus, session, aircraft, freshness and current bindings again.")
            final_ctx = self.context_provider()
            final_actions = {a["id"]: a for a in self.actions_provider()}
            final_action = final_actions.get(item["action_id"])
            reason = self.validation(final_action, final_ctx) if final_action else "Action no longer allowlisted."
            if self._mismatch(item["context"], final_ctx) or reason or not final_ctx.get("focused") or self.clock() >= item["expires_at"]:
                self._transition(item, "Rejected", reason or "Context or focus changed during final validation.")
                return
            if final_action["combo"] != item["action"]["combo"]:
                self._transition(item, "Rejected", "Resolved control changed before send.")
                return
            if final_ctx["model_time"] <= item["settle_model_time"]:
                self._transition(item, "Rejected", "Aircraft model time did not advance while focus settled; paused/menu input is blocked.")
                return
            # Durable claim BEFORE invoking input_sender. A crash here consumes the ID.
            changed_rows = self.db.execute("UPDATE commands SET attempted=1 WHERE id=? AND attempted=0 AND active=1", (item["request_id"],)).rowcount
            self.db.commit()
            if changed_rows != 1:
                self._transition(item, "Unconfirmed", "Request was already attempted; never replayed.")
                return
            try:
                result = self.sender(final_action, item)
            except Exception as exc:
                self._transition(item, "Unconfirmed", f"Ambiguous sender failure ({type(exc).__name__}); no retry.")
                return
            item["sender_result"] = result
            if result.get("dry_run"):
                self._transition(item, "Unconfirmed", "Dry run audited. No input sent; no aircraft state confirmation claimed.")
            elif not result.get("ok") or not result.get("sent"):
                self._transition(item, "Unconfirmed" if result.get("ambiguous") else "Rejected", result.get("message", "Sender refused."))
            else:
                item["sent"] = True
                item["sent_sample_epoch"] = final_ctx.get("sample_epoch") or self.clock()
                item["confirm_until"] = self.clock() + (12 if final_action.get("observable") else .5)
                self._transition(item, "Sent", "Input sender and audit establish one send.")
                self._transition(item, "Confirming", "Waiting for fresh aircraft-specific observable evidence.")

    def stop(self):
        """Wait for any in-flight sender to release keys, then cancel queued work."""
        with self.lock:
            self.stopped = True
            row = self.db.execute("SELECT payload FROM commands WHERE active=1").fetchone()
            if row:
                item = json.loads(row[0])
                self._transition(item, "Cancelled" if item["state"] in PRE_SEND else "Unconfirmed",
                                 "Dashboard stopped. No queued work is replayed on restart.")

    def close(self):
        with self.lock:
            self.stop()
            self.db.close()
