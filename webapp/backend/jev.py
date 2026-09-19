"""Optional TypeSafe Jev advisory integration.

The model selects and scores bounded candidates. This module never queues an
aircraft command. All freshness, geometry, authorization, and execution policy
remain deterministic companion code.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

import httpx

from .jev_questions import (
    CONTACT_CONFIDENCE_MIN,
    DATA_SUFFICIENT_MIN,
    MAX_CONTACT_CHOICES,
    MAX_NAVIGATION_CHOICES,
    QUESTION_SET_VERSION,
    build_questions,
)

API_URL = "https://api.typesafe.ai/v1/systemone"
ALLOWED_INTENTS = {
    "prepare_waypoint", "select_contact", "navigation_help", "checklist",
    "diagnose", "summarize_state", "unsupported",
}


class JevUnavailable(RuntimeError):
    pass


class JevServiceError(RuntimeError):
    pass


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _text(value, limit=120):
    return value[:limit] if isinstance(value, str) and value else None


def _identifier(value, limit=80):
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        rendered = str(value)
        return rendered[:limit] if rendered else None
    return None


def _coordinates(row):
    if not isinstance(row, dict):
        return None
    lat, lon = row.get("lat", row.get("latitude")), row.get("lon", row.get("longitude"))
    if _finite(lat) and _finite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
        return float(lat), float(lon)
    return None


def _navigation(origin, target):
    """Return deterministic great-circle distance and initial true bearing."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*origin, *target))
    dlat, dlon = lat2-lat1, lon2-lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    distance = 3440.065 * 2 * math.asin(math.sqrt(max(0, min(1, a))))
    east = math.sin(dlon)*math.cos(lat2)
    north = math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(dlon)
    bearing = (math.degrees(math.atan2(east, north))+360) % 360
    return round(distance, 2), round(bearing, 1)


def _compact_contact(row, origin):
    if not isinstance(row, dict):
        return None
    identity = _identifier(row.get("id"), 80)
    position = _coordinates(row)
    age = row.get("age_s")
    if not identity or not position or not _finite(age) or not 0 <= age <= 5:
        return None
    distance, bearing = _navigation(origin, position) if origin else (None, None)
    result = {
        "id": identity,
        "name": _text(row.get("name"), 100) or identity,
        "type": _text(row.get("type"), 80) or "unknown",
        "category": _text(row.get("category"), 30) or "unknown",
        "latitude": position[0],
        "longitude": position[1],
        "age_seconds": round(float(age), 2),
    }
    if distance is not None:
        result.update(distance_nm=distance, bearing_true_deg=bearing)
    for source, target in (("altitude_ft", "altitude_ft"), ("speed_kt", "speed_kt"),
                           ("track_true_deg", "track_true_deg"),
                           ("heading_true_deg", "heading_true_deg")):
        if _finite(row.get(source)):
            result[target] = round(float(row[source]), 2)
    return result


def _compact_navigation_item(row):
    if not isinstance(row, dict):
        return None
    identity = _identifier(row.get("id"), 100) or _text(row.get("name"), 100)
    if not identity:
        return None
    result = {"id": identity, "name": _text(row.get("name"), 100) or identity}
    for key in ("type", "runway", "callsign"):
        value = _text(row.get(key), 80)
        if value:
            result[key] = value
    for key in ("distance_nm", "bearing_true_deg", "lat", "lon", "latitude", "longitude"):
        if _finite(row.get(key)):
            result[key] = round(float(row[key]), 5)
    return result


def reduce_snapshot(snapshot, query):
    """Build the only application state permitted to leave the DCS computer."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    health = snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {}
    aircraft = snapshot.get("aircraft") if isinstance(snapshot.get("aircraft"), dict) else {}
    awareness = snapshot.get("awareness") if isinstance(snapshot.get("awareness"), dict) else {}
    navigation = snapshot.get("navigation") if isinstance(snapshot.get("navigation"), dict) else {}
    origin = _coordinates(aircraft) or _coordinates(navigation.get("ownship"))

    contacts = []
    if (awareness.get("status") == "Available" and awareness.get("single_player") is True
            and awareness.get("model_advancing") is True):
        for row in awareness.get("contacts", []):
            contact = _compact_contact(row, origin)
            if contact:
                contacts.append(contact)
            if len(contacts) >= MAX_CONTACT_CHOICES:
                break

    ownship = {}
    for source, target in (
        ("aircraft", "aircraft"), ("latitude", "latitude"), ("longitude", "longitude"),
        ("altitude_ft", "altitude_ft"), ("altitude_m", "altitude_m"),
        ("heading_true_deg", "heading_true_deg"), ("heading_deg", "heading_deg"),
        ("ground_speed_kt", "ground_speed_kt"), ("speed_kt", "speed_kt"),
        ("vertical_speed_fpm", "vertical_speed_fpm"), ("weight_on_wheels", "weight_on_wheels"),
    ):
        value = aircraft.get(source)
        if isinstance(value, (str, bool)) or _finite(value):
            ownship[target] = value

    nearest = []
    for row in navigation.get("nearest", []):
        item = _compact_navigation_item(row)
        if item:
            nearest.append(item)
        if len(nearest) >= MAX_NAVIGATION_CHOICES:
            break

    return {
        "request": query,
        "session": {
            "aircraft": _text(health.get("aircraft"), 80),
            "terrain": _text(health.get("terrain"), 80),
            "status": _text(health.get("status"), 80),
            "telemetry_fresh": health.get("telemetry_fresh") is True,
            "model_advancing": health.get("model_advancing") is True,
            "mission_awareness": _text(awareness.get("status"), 40),
            "single_player_verified": awareness.get("single_player") is True,
            "mission_awareness_age_seconds": awareness.get("age_s") if _finite(awareness.get("age_s")) else None,
        },
        "ownship": ownship,
        "contacts": contacts,
        "nearby_navigation": nearest,
    }


def contact_criteria(contacts):
    criteria = {"none": "No supplied contact matches the request."}
    for contact in contacts:
        description = f"{contact['name']}; {contact['type']}; category {contact['category']}"
        if "distance_nm" in contact:
            description += f"; {contact['distance_nm']} NM at {contact['bearing_true_deg']} degrees true"
        criteria[contact["id"]] = description
    return criteria


def _answer(answers, key, expected):
    answer = answers.get(key)
    return answer if isinstance(answer, dict) and answer.get("type") == expected else {}


def compose_advisory(state, response, elapsed_ms):
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        raise JevServiceError("Jev returned an invalid answer map.")
    intent = _answer(answers, "intent", "choice")
    selected = _answer(answers, "selected_contact", "choice")
    sufficient = _answer(answers, "data_sufficient", "noul")
    phase = _answer(answers, "flight_phase", "choice")
    workload = _answer(answers, "workload", "score")
    intent_value = intent.get("choice") if intent.get("choice") in ALLOWED_INTENTS else "unsupported"
    selected_id = selected.get("choice") if isinstance(selected.get("choice"), str) else "none"
    candidate = next((row for row in state["contacts"] if row["id"] == selected_id), None)
    contact_confidence = selected.get("confidence") if _finite(selected.get("confidence")) else 0.0
    sufficient_probability = sufficient.get("noul") if _finite(sufficient.get("noul")) else 0.0
    contact_intent = intent_value in {"prepare_waypoint", "select_contact"}
    proposal_allowed = bool(
        contact_intent and candidate and contact_confidence >= CONTACT_CONFIDENCE_MIN
        and sufficient_probability >= DATA_SUFFICIENT_MIN
        and state["session"]["telemetry_fresh"]
        and state["session"]["single_player_verified"]
        and state["session"]["mission_awareness"] == "Available"
        and candidate["age_seconds"] <= 5
    )

    if proposal_allowed:
        summary = f"Selected {candidate['name']} as a display-only contact proposal."
    elif contact_intent:
        summary = "No contact proposal passed the companion's deterministic evidence gates."
    elif intent_value == "unsupported":
        summary = "This request does not match a supported read-only companion operation."
    else:
        summary = f"Jev classified this as {intent_value.replace('_', ' ')}."

    return {
        "schema": "dcs-jev-advisory/1",
        "question_set": QUESTION_SET_VERSION,
        "model": _text(response.get("model"), 80) or "unknown",
        "latency_ms": round(elapsed_ms, 1),
        "summary": summary,
        "intent": {
            "value": intent_value,
            "confidence": intent.get("confidence"),
            "probabilities": intent.get("probabilities", {}),
        },
        "flight_phase": {
            "value": phase.get("choice", "unknown"),
            "confidence": phase.get("confidence"),
            "probabilities": phase.get("probabilities", {}),
        },
        "workload": {
            "score": workload.get("score"),
            "confidence": workload.get("confidence"),
            "legend": workload.get("legend", {}),
            "probabilities": workload.get("probabilities", {}),
        },
        "data_sufficient_probability": sufficient_probability,
        "contact": candidate if proposal_allowed else None,
        "contact_confidence": contact_confidence if contact_intent else None,
        "proposal_allowed": proposal_allowed,
        "execution": "display-only",
        "usage": response.get("usage", {}),
    }


class JevAdvisor:
    def __init__(self, settings, *, transport=None, clock=time.monotonic):
        self.api_key = settings.jev_api_key
        self.enabled = settings.jev_enabled
        self.mode = settings.jev_mode
        self.model = settings.jev_model
        self.timeout_s = settings.jev_timeout_s
        self.audit_path = Path(settings.runtime_dir) / "jev-audit.jsonl"
        self.transport = transport
        self.clock = clock
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._cache = {}

    def status(self):
        return {
            "enabled": self.enabled,
            "configured": bool(self.api_key) or self.mode == "mock",
            "mode": self.mode,
            "model": self.model,
            "question_set": QUESTION_SET_VERSION,
            "execution": "display-only",
        }

    async def _post(self, payload):
        if self.transport:
            return await self.transport(payload)
        timeout = httpx.Timeout(self.timeout_s, connect=min(5.0, self.timeout_s))
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(2):
                response = await client.post(API_URL, headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "dcs-companion/jev-1",
                }, json=payload)
                if response.status_code not in (429, 529) or attempt:
                    break
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(0.25, min(2.0, float(retry_after)))
                except ValueError:
                    delay = 0.5
                await asyncio.sleep(delay)
        if response.status_code in (429, 529):
            raise JevServiceError("Jev is busy or rate limited. Try again shortly.")
        if response.status_code == 401:
            raise JevUnavailable("The TypeSafe API key was rejected.")
        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JevServiceError("Jev returned an unreadable response.") from exc

    def _mock(self, state):
        has_contact = bool(state["contacts"])
        return {
            "model": "jev-mock",
            "answers": {
                "intent": {"type": "choice", "choice": "select_contact" if has_contact else "diagnose",
                           "confidence": .92, "probabilities": {}},
                "selected_contact": {"type": "choice", "choice": state["contacts"][0]["id"] if has_contact else "none",
                                     "confidence": .9 if has_contact else 1.0, "probabilities": {}},
                "data_sufficient": {"type": "noul", "noul": .9 if has_contact else .7},
                "flight_phase": {"type": "choice", "choice": "unknown", "confidence": .4, "probabilities": {}},
                "workload": {"type": "score", "score": .5, "confidence": .7,
                             "legend": {"0": "Low", "1": "Moderate", "2": "High"}, "probabilities": {}},
            },
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    def _audit(self, query_hash, advisory):
        event = {
            "timestamp": time.time(), "query_sha256": query_hash,
            "question_set": QUESTION_SET_VERSION, "model": advisory["model"],
            "intent": advisory["intent"]["value"],
            "proposal_allowed": advisory["proposal_allowed"],
            "latency_ms": advisory["latency_ms"], "usage": advisory.get("usage", {}),
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError:
            pass

    async def evaluate(self, query, snapshot):
        if not self.enabled:
            raise JevUnavailable("Jev advisories are disabled.")
        if self.mode != "mock" and not self.api_key:
            raise JevUnavailable("Set TYPESAFE_API_KEY on the DCS computer to enable Jev.")
        state = reduce_snapshot(snapshot, query)
        questions = build_questions(contact_criteria(state["contacts"]))
        payload = {"model": self.model, "state": state, "questions": questions}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        cached = self._cache.get(fingerprint)
        now = self.clock()
        if cached and now-cached[0] <= 5:
            return {**cached[1], "cached": True}
        async with self._lock:
            cached = self._cache.get(fingerprint)
            now = self.clock()
            if cached and now-cached[0] <= 5:
                return {**cached[1], "cached": True}
            delay = 1.5-(now-self._last_call)
            if delay > 0:
                await asyncio.sleep(delay)
            started = self.clock()
            try:
                response = self._mock(state) if self.mode == "mock" else await self._post(payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise JevServiceError("Jev is unreachable or timed out.") from exc
            finally:
                self._last_call = self.clock()
            advisory = compose_advisory(state, response, (self.clock()-started)*1000)
            advisory["cached"] = False
            self._cache = {fingerprint: (self.clock(), advisory)}
            self._audit(hashlib.sha256(query.encode()).hexdigest(), advisory)
            return advisory
