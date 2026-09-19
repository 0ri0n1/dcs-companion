# Automatic startup

Open **Enable Auto Start.cmd** once. It adds the uniquely named **DCS Companion Auto Start.lnk** to your Windows account's Startup folder and starts a hidden watcher now. No administrator rights, DCS changes, Saved Games edits, scheduled tasks, or registry edits are needed. **Disable Auto Start.cmd** removes only that shortcut and asks the watcher to exit; DCS and the dashboard are left running.

The watcher uses the discovered or selected DCS installation and checks the executable path, PID and creation time. An unrelated program named DCS.exe cannot trigger startup. It checks every two seconds while idle. During a DCS session it checks the dashboard and collector every five seconds.

On a new DCS session it starts or repairs the existing local dashboard and collector, then requests one browser window. Browser-launch intent is recorded before opening it, so restarting the watcher, reconnecting the page or recovering the backend does not open another window in the same DCS session. A browser launch that fails ambiguously is not repeated automatically; use **Start Dashboard.cmd** to reopen it manually. The watcher never starts DCS.

Automatic starts always use **dry-run controls** on **127.0.0.1**. Existing manually started live/LAN dashboards retain their current mode. If one crashes, recovery uses dry-run loopback; it never recreates live control or LAN access automatically. The browser shows its current mode. A changed LAN address may require opening the local dashboard explicitly.

Closing DCS leaves the dashboard and collector available for debrief. The idle watcher does not recover components while DCS is closed. This preserves the end-of-flight view and avoids an unexpected shutdown. Use **Stop Dashboard.cmd** when finished.

Manual Stop is respected: it writes a pause marker before stopping owned processes. The watcher will not restart the dashboard for that DCS process. Starting a new DCS process/session resumes automatic startup. Explicit **Start Dashboard.cmd** clears the pause marker. The backend receives its existing graceful stop request; an active input send is not forcibly interrupted. Only a collector started and still owned by this launcher can be stopped.

Failures retry with backoff: 5, 10, 20, 40, 80 and then 120 seconds. Foreign or ambiguous owners of UDP 17781 or TCP 18787 block startup; no unrelated process is killed. A collector created by this launcher is rolled back if another process races it to the UDP port. One lock prevents concurrent launcher mutations and another prevents duplicate watchers. Tracking files use atomic replacement and corrupt/missing JSON falls back safely to actual port/process checks. Unknown ownership is never adopted for stopping.

## Status and maintenance

The watcher writes `runtime/autostart-status.json` with schema `dcs-companion/autostart/1`: `enabled`, `watcher_pid`, `updated_epoch`, `state`, `dcs_session`, `dashboard_url`, `last_error`, `next_retry_epoch`, `opened_for_session`, and `policy`. States include Waiting for DCS, Starting dashboard, Watching DCS, Paused by manual stop, Retrying, Debrief and Disabled. `updated_epoch` is a heartbeat, written on transitions or at least every ten seconds while running. Old status files do not establish that the watcher is alive; check PID/heartbeat.

`runtime/autostart.log` rotates at 256 KB and retains two previous files. Child console logs rotate before a new process starts if over 2 MB; the collector's active console log can grow during a long session. `launcher-processes.json` records only processes started by this launcher, including identity checks. `manual-stop.json`, `autostart-session.json` and `browser-session.json` preserve intentional stop and browser behavior across watcher restarts.

Install/start explicitly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\src\dcs-companion\webapp\tools\install_autostart.ps1" -StartNow
```

Disable explicitly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\src\dcs-companion\webapp\tools\install_autostart.ps1" -Uninstall
```

Inspect status without starting anything:

```powershell
& "C:\src\dcs-companion\webapp\.venv\Scripts\python.exe" "C:\src\dcs-companion\webapp\autostart.py" status
```

MEASURED (automated simulation): the tests exercise exact-path DCS recognition, new/reused PID sessions, no duplicate launch, crash backoff, debrief behavior, intentional Stop, port conflicts, source process identity, missing/corrupt tracking files, retained collector ownership and forced dry-run recovery. They do not launch DCS or press any input. Installation/current-session startup verification and live DCS session verification must be reported separately. This feature is **Not yet flight-tested**.
