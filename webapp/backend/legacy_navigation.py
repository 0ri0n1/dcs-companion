"""Compatibility shape for existing dcs_navigation callers, using the shared database."""
from .navigation import NavigationService, ownship_navigation


def legacy_navigation(state, max_results=5):
    nav = NavigationService().snapshot(state)
    own = nav["ownship"]
    ac = state.get("aircraft") or {}
    live = own["fresh"]
    fields = []
    for field in nav["nearest"][:max(0, min(100, int(max_results)))]:
        heading = own.get("heading_true_deg")
        bearing = field["bearing_true_deg"]
        turn = ((bearing - heading + 540) % 360) - 180 if heading is not None else None
        fields.append({"field": field["name"], "id": field["id"], "bearing_deg": round(bearing),
                       "bearing_reference": "TRUE", "range_nm": round(field["distance_nm"], 1),
                       "eta_minutes": round(field["eta_seconds"] / 60, 1) if live and field.get("eta_seconds") is not None else None,
                       "turn_deg": round(turn) if turn is not None else None,
                       "turn_direction": "right" if turn is not None and turn > 0 else "left" if turn is not None else None,
                       "position_confidence": field.get("position_confidence", "unknown")})
    return {"stream": {"live": live, "data_age_seconds": own["telemetry_age_seconds"],
                       "warning": None if live else "NO RECENT DATA. Navigation values use a last-known position; ETA withheld."},
            "position": {"latitude": ac.get("latitude"), "longitude": ac.get("longitude"),
                         "heading_deg": ac.get("heading_deg"), "ias_kt": ac.get("ias_kt"),
                         "ground_speed_kt": own.get("ground_speed_kt"), "altitude_msl_ft": ac.get("altitude_msl_ft")},
            "airfields": fields, "terrain": nav["terrain"], "terrain_detection": nav["detection"],
            "database_version": nav.get("provenance", {}).get("dcs_version"),
            "caveat": "Ground-speed ETA only. Terrain ambiguity returns no airfields. Airport reference coordinates retain their stated provenance; no localizer is used as a runway centerline.",
            "source": "webapp versioned navigation database"}
