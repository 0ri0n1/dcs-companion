"""Bounded, loss-visible capture of received exporter packets. Standard library only."""
from collections import OrderedDict
import copy
import json
from pathlib import Path
import time


class ExportCapture:
    def __init__(self, directory=None, *, file_bytes=16 * 1024 * 1024, files=4,
                 latest_bytes=4 * 1024 * 1024, latest_slots=128):
        self.directory = Path(directory) if directory else None
        self.file_bytes, self.files = file_bytes, files
        self.latest_bytes, self.latest_slots = latest_bytes, latest_slots
        self.latest = OrderedDict()
        self.sizes = {}
        self.received = self.evicted = self.write_failures = 0
        self.archived_packets = 0
        self.invalid_packets = 0
        self.last_invalid_error = None
        self.error = None
        self.handle = None
        self.size = 0
        self.flush_at = self.retry_at = 0

    def clear_latest(self):
        self.latest.clear()
        self.sizes.clear()

    @staticmethod
    def validation_error(packet):
        if not isinstance(packet, dict):
            return "Exporter packet must be a JSON object"
        if not isinstance(packet.get("type", "?"), str):
            return "Exporter packet type must be a string"
        try:
            json.dumps(packet, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            return "Invalid packet JSON: " + str(exc)[:300]
        return None

    def reject(self, reason):
        """Count invalid received input without claiming it reached the archive."""
        self.received += 1
        self.invalid_packets += 1
        self.last_invalid_error = reason
        return False

    def _remember(self, key, item, size):
        self.latest.pop(key, None)
        self.sizes.pop(key, None)
        if size <= self.latest_bytes:
            self.latest[key], self.sizes[key] = item, size
        else:
            self.evicted += 1
        while len(self.latest) > self.latest_slots or sum(self.sizes.values()) > self.latest_bytes:
            old, _ = self.latest.popitem(last=False)
            self.sizes.pop(old)
            self.evicted += 1

    def associate_startup(self, items, session, identity):
        """Associate already archived startup diagnostics; never duplicate receipt.

        Caller validates a fresh matching probe and bounds the candidate set.
        Original receipt epochs and archive records remain unchanged.
        """
        for key, original in items.items():
            item = copy.deepcopy(original)
            item["received_session"] = item["session"]
            item["session"] = session
            item["aircraft"], item["unit_name"] = identity
            item["association"] = "First matching telemetry established startup aircraft identity"
            size = len((json.dumps(item, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8"))
            self._remember(key, item, size)

    def _close(self):
        if self.handle:
            try:
                self.handle.close()
            finally:
                self.handle = None

    def _append(self, data, now):
        if not self.directory:
            return
        if now < self.retry_at:
            self.write_failures += 1
            return
        try:
            if len(data) > self.file_bytes:
                raise OSError("Packet exceeds the configured archive file limit")
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / "packets.jsonl"
            if not self.handle:
                self.size = path.stat().st_size if path.exists() else 0
                self.handle = path.open("ab")
            if self.size + len(data) > self.file_bytes:
                self._close()
                for index in range(self.files - 1, 0, -1):
                    source = path if index == 1 else self.directory / f"packets.{index-1}.jsonl"
                    target = self.directory / f"packets.{index}.jsonl"
                    if source.exists():
                        source.replace(target)
                self.handle = path.open("wb")
                self.size = 0
            self.handle.write(data)
            self.size += len(data)
            if now >= self.flush_at:
                self.handle.flush()
                self.flush_at = now + 1
            self.error = None
            self.archived_packets += 1
        except OSError as exc:
            self.error = str(exc)
            self.write_failures += 1
            self.retry_at = now + 5
            try:
                self._close()
            except OSError:
                pass

    def flush_due(self, now=None):
        """Flush the last burst even when the simulator pauses and sends no more."""
        now = time.time() if now is None else now
        if not self.handle or now < self.flush_at:
            return
        try:
            self.handle.flush()
            self.flush_at = now + 1
        except OSError as exc:
            self.error = str(exc)
            self.write_failures += 1
            self.retry_at = now + 5
            try:
                self._close()
            except OSError:
                pass

    def record(self, packet, epoch, session, identity):
        """Keep complete JSON, including unknown types/fields; no semantic rewriting."""
        invalid = self.validation_error(packet)
        if invalid:
            return self.reject(invalid)
        kind = str(packet.get("type", "?"))
        key = kind + (":" + str(packet.get("id", "?")) if kind == "indications" else "")
        item = {"received_epoch": epoch, "session": session,
                "aircraft": identity[0] if identity else None,
                "unit_name": identity[1] if identity else None, "packet": copy.deepcopy(packet)}
        try:
            encoded = (json.dumps(item, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            return self.reject("Invalid capture metadata: " + str(exc)[:300])
        self.received += 1
        self._append(encoded, epoch)
        self._remember(key, item, len(encoded))
        return True

    def snapshot(self):
        return {"schema": "dcs-companion/export-capture/1", "latest": dict(self.latest),
                "recording": {"status": "Error" if self.error else ("Recording" if self.archived_packets else "Waiting for packets") if self.directory else "Memory only",
                              "directory": str(self.directory) if self.directory else None,
                              "max_bytes": self.file_bytes * self.files, "files": self.files,
                              "received_packets": self.received, "write_failures": self.write_failures,
                              "archived_packets": self.archived_packets,
                              "invalid_packets": self.invalid_packets, "last_invalid_error": self.last_invalid_error,
                              "latest_evictions": self.evicted, "error": self.error}}

    def close(self):
        self._close()
