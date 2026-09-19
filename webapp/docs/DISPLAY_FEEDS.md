# Live Hornet display images

**Remote → All displays** shows the F/A-18C's Left DDI, AMPCD and Right DDI in cockpit order on desktop. Phones stack the same panels with their bezel controls. Each panel offers **Live stream** and **Text**, plus fullscreen where supported. Text is the separately aged exporter readout; it does not require image capture.

DCS renders native monitor exports into a dedicated display area. The companion captures only the registered panel regions within that approved, unobscured export rectangle and streams JPEG frames to paired viewers. Enabling capture does not enable cockpit inputs. See [Remote controls](REMOTE.md) for bezel operation and aircraft layout support.

## Requirements and monitor arrangement

Complete [telemetry setup](INSTALL.md) and select the correct DCS installation and Saved Games profile in **Your setup** first. Native image capture currently requires a Hornet session and a verified export layout.

Use either:

- An eligible **virtual display**, keeping physical monitors available for other applications.
- A **dedicated physical display**, which reserves the selected screen for DCS exports.

The main display and export display must share a fully aligned horizontal or vertical edge, without a gap or overlap. Other monitors must stay outside their combined DCS render rectangle. Disable DCS VR and select the standard single-camera monitor profile before preparing a new layout. The companion checks the installed Hornet viewport definitions and the actual monitor arrangement.

Virtual displays are identified by their registered adapter hardware identity, not their friendly name. The current supported virtual identity is `Root\\MttVDD`. Display drivers are **not bundled**. Install and arrange a compatible driver separately; the browser does not install drivers or move Windows screens.

The low-level `tools/install_virtual_display.ps1` and `tools/uninstall_virtual_display.ps1` helpers expect specific, externally staged driver/installer payloads and local installation records under `runtime/virtual-display-setup/`. Installation also requires a measured display checkpoint. These helpers are not a general download-and-install workflow; review their pinned inputs and prerequisites before use.

## Prepare and enable images

1. On the DCS computer, open **Remote → Live display images**. Choose an eligible **Dedicated export monitor** and select **Prepare display layout**. Preparing a review does not change DCS configuration.
2. Review the monitor arrangement and settings. **Close DCS completely**, tick the review checkbox, then choose **Apply reviewed layout**. This backs up and changes the selected profile's display options and Companion monitor profile; it does not edit aircraft bindings or the DCS installation.
3. Start DCS again and enter a Hornet cockpit. DCS must load the applied layout in a new process, and its client area must match the reviewed arrangement.
4. On the computer, choose **Enable display capture**, then return focus to DCS. Capture starts disabled and resets when the companion restarts. Paired phones can view enabled images, but cannot apply the layout or enable capture.
5. Open **Remote → All displays** on the computer or paired device. Choose Live stream or Text for each panel. Source choices are retained between overview and individual panels.

For phone pairing and any separate firewall setup, see [Connections](CONNECTIONS.md).

## Streaming and freshness

One shared capture producer serves all viewers, targeting **15 frames per second per panel**. Actual refresh depends on the PC, GPU, network and browser; this is not a guaranteed delivery rate. Each browser uses one continuous stream for its selected panels and decodes the newest waiting frame instead of building a playback queue. Leaving Remote, hiding the page or switching every panel to Text closes that viewer's stream.

Frames carry capture time, revision and sequence metadata. Images older than the allowed two-second age, a lost stream heartbeat, or a changed aircraft/session/revision are cleared. Bounded read-only reconnection does not retry cockpit commands.

Capture requires matching DCS process/profile identity, fresh advancing telemetry, foreground focus and valid window/monitor geometry. Pausing, minimizing, resizing, rearranging monitors or covering an export region can withhold images. Native focus, geometry and coverage checks run for each capture. Configuration/layout validation has a short bounded reuse window; failed or expired validation does not authorize further capture.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No export display is eligible | Check Windows arrangement, adapter identity and the required shared edge; then select **Refresh display setup**. |
| Layout requires restart | Close and restart DCS after applying. Restarting only the companion does not load DCS monitor settings. |
| Capture is off after a restart | Enable capture again on the PC. This is independent of live-control enable. |
| Images pause while using the PC browser | Return foreground focus to DCS and keep the export area unobscured. A phone can remain the viewer. |
| Images disappear after a resize or monitor change | Check the reported geometry/layout reason and review the arrangement before enabling capture. |
| A panel is black or appears frozen | Compare with that actual DCS display. It may be blank/static, or capture may be returning an old surface. Sequence numbers alone cannot distinguish these cases. |
| Text is fresh but images are unavailable | Text export and native capture have separate sources and prerequisites. Check image-capture status rather than treating text freshness as video proof. |

## Restore the previous layout

Close DCS, then choose **Restore previous display layout** on the computer. Backups are stored locally under `webapp/runtime/display-export/backups/`.

Restoration preserves unrelated options changed since setup and can remain available after monitor attachment or arrangement changes. Unexpected edits to the owned display settings/files stop automatic restoration for review. Restart DCS after restoration. Restore its layout before removing a virtual display through the recorded uninstall helper.

## Validate actual motion

Delivery and changing image content are separate checks. Increasing sequence numbers, recent capture timestamps and successful image decoding prove transport activity, not that Windows supplied newly rendered pixels.

Choose a display known to be changing in DCS. Validate native image-content changes and decoded browser-pixel changes, while also measuring delivery gaps, freshness and stream revision. A legitimately static page is allowed. JPEG hashes can establish differing native captures; decoded-pixel comparisons establish what the browser rendered. Neither a few different frames nor sequence throughput alone proves the visual frame rate.

Automated fixtures exercise application behavior, but do not establish native GPU capture freshness or physical-phone performance. Report transport rate and motion evidence separately.
