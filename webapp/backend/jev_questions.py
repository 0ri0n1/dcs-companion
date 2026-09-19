"""Reviewable Jev questions and policy constants.

Keep semantic questions and thresholds together. Typed output guarantees the
interface, not correctness; these values must be evaluated against recorded DCS
states before they are relaxed.
"""

QUESTION_SET_VERSION = "dcs-jev/1"

CONTACT_CONFIDENCE_MIN = 0.70
DATA_SUFFICIENT_MIN = 0.75
MAX_CONTACT_CHOICES = 100
MAX_NAVIGATION_CHOICES = 40

INTENT_CRITERIA = {
    "prepare_waypoint": (
        "Prepare a display-only waypoint proposal from one supplied mission contact."
    ),
    "select_contact": "Find or inspect one supplied mission contact.",
    "navigation_help": "Inspect the route, nearby airfields, or navigation state.",
    "checklist": "Choose context for a pilot-operated checklist or procedure.",
    "diagnose": "Diagnose DCS Companion telemetry, setup, display, or connection state.",
    "summarize_state": "Summarize the current companion state without taking an action.",
    "unsupported": "No supported read-only companion operation matches the request.",
}

FLIGHT_PHASE_CRITERIA = {
    "cold_and_dark": "Aircraft is stationary and not operating for flight.",
    "startup": "Aircraft systems are being prepared before taxi.",
    "taxi": "Aircraft is moving on the ground before takeoff or after landing.",
    "takeoff": "Aircraft is beginning the takeoff roll or initial liftoff.",
    "climb": "Aircraft is airborne and gaining altitude toward cruise or mission altitude.",
    "cruise": "Aircraft is airborne in relatively stable, non-terminal flight.",
    "descent": "Aircraft is airborne and descending but not yet on final approach.",
    "approach": "Aircraft is configuring or positioning for landing.",
    "landing": "Aircraft is on final landing segment, touchdown, or landing rollout.",
    "unknown": "The supplied state does not establish a flight phase.",
}

WORKLOAD_LEVELS = [
    "Low workload: stable state and a passive display-only request.",
    "Moderate workload: changing state or a request requiring several pilot interactions.",
    "High workload: terminal flight phase, time pressure, ambiguity, or conflicting demands.",
]


def build_questions(contact_criteria):
    """Return independent questions evaluated in one speculative fan-out."""
    return {
        "intent": {
            "type": "choice",
            "instructions": "Which supported operation is requested in `request`?",
            "criteria": INTENT_CRITERIA,
        },
        "selected_contact": {
            "type": "choice",
            "instructions": (
                "If `request` refers to a mission contact, which entry in `contacts` "
                "best matches it? Choose none when the request is unrelated or no "
                "supplied contact matches."
            ),
            "criteria": contact_criteria,
        },
        "data_sufficient": {
            "type": "noul",
            "instructions": (
                "Does the supplied state contain enough fresh, internally consistent "
                "evidence to answer `request` without guessing?"
            ),
            "criteria": {
                "true": "Required evidence is present, fresh, and has one supported interpretation.",
                "false": "Required evidence is missing, stale, contradictory, ambiguous, or unsupported.",
            },
        },
        "flight_phase": {
            "type": "choice",
            "instructions": (
                "Which flight phase is best supported by `session` and `ownship`? "
                "Choose unknown when the evidence is insufficient."
            ),
            "criteria": FLIGHT_PHASE_CRITERIA,
        },
        "workload": {
            "type": "score",
            "instructions": (
                "Estimate pilot workload using only `request`, `session`, and `ownship`."
            ),
            "criteria": WORKLOAD_LEVELS,
        },
    }
