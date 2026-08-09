# Remote MCP access

DevMCP Runtime serves MCP over loopback by default. For ChatGPT Developer Mode,
install the official Secure MCP Tunnel client separately, then configure the
tunnel ID and runtime API key through `devmcp setup` or the loopback UI.

`devmcp service install` runs `devmcp serve` and foreground
`tunnel-client run --profile …` as separate systemd user services. It does not
use `tunnel-client runtimes connect`, so the native runtime supervisor is never
wrapped in a second infinite supervisor. The tunnel key is passed using its
supported `file:/path` reference. The foreground daemon writes a private,
loopback health URL under the DevMCP config directory; `devmcp status` probes
it with `tunnel-client health --require-control-plane-poll` for actual
readiness.

Run `devmcp tunnel doctor` before starting the tunnel and `devmcp status` to
see local MCP health and tunnel readiness when the tunnel client can report it.

## Migrating an existing local service

Run `devmcp service install` once after upgrading; it rewrites the DevMCP user
units to the stable config-driven launchers. Then run `devmcp restart`, which
loads the current `config.toml`. If an older external installer created an
additional legacy user unit, stop and disable that unit before enabling the
new pair so two runtimes do not compete for the MCP port. Configuration and
secret files are preserved.
