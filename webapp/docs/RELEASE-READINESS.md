# Repository status

The initial repository is private. It packages the current Companion source,
collector, guarded input sender, source exporter, optional MCP reader, tests and
documentation. Personal runtime files, binding assignments, generated terrain
catalogues, screenshots, logs, copied manuals and driver binaries are excluded.

The existing local flight setup is separate from this repository checkout and was
not restarted or reconfigured during packaging.

Before making a public release:

- Select the intended project licence and review the source/dependency notices.
- Validate installation on another Windows machine, including export chaining.
- Finish and validate the optional mission-bridge and virtual-display installation
  story; these external components are not bundled or installed by basic setup.
- Verify the physical phone/Safari experience and changing display imagery on the
  supported hardware. Automated emulation does not establish those results.
- Review the staged history and any new screenshots for private information.

See [installation](INSTALL.md), [development](DEVELOPMENT.md), and
[project notices](../../NOTICE.md). Private publication is a source checkpoint,
not a claim that every optional integration is a turnkey public release.
