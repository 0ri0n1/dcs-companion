# Connection history and phone pairing

## Find an interruption

Open **Data Health → Connection history**. The panel separates browser transport, last received update, telemetry availability and mission-awareness availability. A working browser connection can still have stale DCS telemetry or unavailable mission enemies. Download the connection report to capture the browser timeline and the server's current diagnostic events.

Browser history keeps up to 200 sanitized events in session storage for that tab. It contains fixed reasons, times and durations, not mission positions, packet contents, passwords, pairing tickets or URLs. It survives reloads in the same tab. It cannot reconstruct disconnects from before this feature was installed.

The server writes `webapp/runtime/connection-events.jsonl`, rotating two backups at about 1 MiB each. It records backend starts, requested stops, stream opens/closes/errors, slow snapshot generation and telemetry/mission-awareness transitions. A stream closure can mean a tab closed or reloaded; it does not by itself prove a network fault. The authenticated `/api/diagnostics` response includes the latest 200 events from the current backend process. A report-download failure leaves browser evidence available.

Other existing logs are `runtime/dashboard-console.log`, `runtime/collector-console.log` and `runtime/autostart.log`. They cover backend exceptions, collector activity/state-write errors and automatic-start recovery. These older logs do not establish the timing of every browser disconnect. The app-only reloads around 16:26 and 16:28 on 2026-09-18 caused expected interruptions; DCS and the collector retained their process identities.

## Connection behavior

The live stream is primary. Backup requests are used when the stream is absent or overdue, rather than continuously duplicating it. Requests have a bounded timeout; an older request cannot overwrite newer streamed data or mark that newer stream offline. Six seconds without stream updates triggers recovery. Ownship/display data still expires at three seconds, and mission-awareness contacts at five seconds. Reconnection never submits cockpit commands.

Repeated parsing/hashing of unchanged DCS log files is avoided using file identity, length and nanosecond timestamps. Watched Lua binding sources still receive full content hashes; command validity does not rely on a timed fingerprint cache.

## Pair an iPhone

Start **Start Tablet Dashboard.cmd** on the DCS computer and choose its private Wi-Fi/Ethernet address. Tablet mode keeps both that specific LAN address and loopback available; it does not listen on a wildcard interface. Open the companion locally and choose **Pair phone**. Keep the iPhone on the same network, scan the QR code with Camera and open it in Safari.

QR codes expire after 120 seconds and can be redeemed once. Use **Create new QR code** if one expires or was already used. The code carries a random invitation in the URL fragment, which the page removes before API requests. The fragment never contains the main manual pairing secret, and is not kept in application history/storage/logs. The server retains only a hash of each pending invitation, limits pending codes and rate-limits failed exchanges. Successful pairing uses an HttpOnly/SameSite cookie. Backend restarts clear sessions and pending codes; the phone must pair again.

The QR is generated locally. No external QR service receives the address or invitation. Phone access uses the existing trusted-local-network HTTP mode. Pairing does not enable live aircraft inputs or macros; Observe/dry-run remains the default.

If Windows blocks phone access, `tools/enable_tablet_firewall.ps1` is an explicit administrator step. It creates only the companion's named rule for TCP 18787, the selected interpreter/address/adapter, Windows Private profile and local-subnet peers. It does not change existing Python/DCS rules, open public interfaces or configure router forwarding. Removing the `DCSCompanion-Tablet-18787` rule reverses that step.
