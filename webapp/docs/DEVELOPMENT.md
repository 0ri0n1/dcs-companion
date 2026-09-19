# Development and checks

Use a source checkout separate from a running flight setup. Runtime state, device
catalogues, pairing records, navigation caches, traces, and logs are ignored.

From the repository root, install and build without starting DCS or the backend:

```powershell
python -m venv webapp\.venv
webapp\.venv\Scripts\python.exe -m pip install -r webapp\requirements-lock.txt
Set-Location webapp\frontend
npm ci
npm run build
```

Run the backend suite from `webapp`:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Tests use synthetic data for core behavior. Checks that inspect a particular
installed DCS build, Lua runtime, binding profile, or generated terrain library are
opt-in with `DCS_LOCAL_INTEGRATION=1`; they are not clean-checkout prerequisites.
The repository's default test run does not send cockpit inputs.

Run browser tests from `webapp/frontend`:

```powershell
npx playwright install chromium
npm test
```

Local tests normally use installed Chrome. GitHub Actions uses Playwright Chromium.
For the same browser locally, set `CI=1` for that command. The fixture-driven suite
covers the phone UI with touch emulation; it does not establish physical iPhone,
Safari, Wi-Fi, DCS rendering, or actual control delivery compatibility.

GitHub Actions runs the backend tests and frontend build/browser tests on Windows.
Live flight validation remains separate: current telemetry, actual changing panel
pixels, the pilot's physical phone, and the real cockpit response must be observed.

When adding files, inspect the staged list before committing. Never force-add
runtime state, generated DCS data, authentication material, Saved Games scripts,
module source, manuals, or dependency/driver binaries.
