# Jev copilot

Jev is an optional online judgment layer for DCS Companion. The dashboard continues to work without it. When enabled, the **Jev Copilot** view can classify a request, estimate flight phase and workload, and select one fresh single-player Mission Awareness contact from a bounded list.

Jev output is advisory. It cannot enqueue a cockpit command, program the aircraft, or bypass the companion's existing input safeguards. Bearings and distances are calculated locally; Jev only makes the bounded semantic judgments.

## Enable it

Set the API key in the environment that launches the dashboard, then restart it:

```powershell
$env:TYPESAFE_API_KEY = '<your TypeSafe API key>'
.\webapp\Start Dashboard.cmd
```

The presence of the key enables Jev by default. To keep the key configured while disabling calls, set `DCS_DASH_JEV=NO`. Optional settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DCS_DASH_JEV` | `YES` when a key exists | Enables or disables Jev |
| `DCS_DASH_JEV_MODEL` | `jev-latest` | TypeSafe model alias |
| `DCS_DASH_JEV_TIMEOUT` | `12` | Request timeout in seconds, clamped to 2–30 |
| `DCS_DASH_JEV_MODE` | `live` | Use `mock` only for local tests |

Do not put the key in source files, browser storage, screenshots, or commits. The browser never receives it.

## Data boundary

Each evaluation sends only a reduced snapshot:

- the request text;
- aircraft/terrain session status and freshness;
- basic ownship position and motion fields;
- verified, fresh single-player Mission Awareness contacts; and
- nearby navigation candidates.

It excludes local paths, input bindings, display text, logs, credentials, raw telemetry packets, and the full collector snapshot. The reduction is implemented in `backend/jev.py` and covered by regression tests.

The reviewable questions and thresholds live together in `backend/jev_questions.py`. A contact proposal requires both Jev thresholds and local deterministic gates for fresh telemetry, verified single-player context, available Mission Awareness, and contact age. Queries are cached briefly and rate-limited. The local audit file stores a query hash and decision metadata, not the request text or API key.

## Development

Use mock mode for deterministic UI and backend tests:

```powershell
$env:DCS_DASH_JEV = 'YES'
$env:DCS_DASH_JEV_MODE = 'mock'
.\webapp\.venv\Scripts\python.exe -m pytest -c webapp\pytest.ini webapp\tests -q
```

Live evaluations require outbound HTTPS access to `api.typesafe.ai`. HTTP 429 and 529 responses receive one bounded retry; service failures return an advisory error and never fall through to aircraft input handling.
