# Security policy

DevMCP Runtime's beta Linux security boundary is **bubblewrap (`bwrap`)**.
Normal execution creates a constrained sandbox; it is not ordinary unrestricted
host-process execution. `unsafe` is an explicit, visible host mode and should
be used only in an isolated VM or container that the operator controls.

## Runtime boundary

- A configured workspace and canonical, no-symlink path handling constrain file
  and patch tools.
- On Linux, `bwrap` is the primary execution boundary. Landlock is additional
  defense in depth where the host supports it.
- The default HTTP and local admin UI binds are loopback-only. A public bind
  also requires authentication and an active `server.public` policy decision.
- Tokens live in 0600 files outside the workspace. The UI masks secret values
  and saving Setup never rotates the MCP connector token.
- `safe`, `balanced`, `power`, and `custom` capability rules are authoritative
  for runtime operations. `ask` creates a scoped, expiring approval; legacy
  `safe`/`trusted`/`dangerous` flags are compatibility presets only when no
  profile was selected.

## Residual risk and platform limits

Sandboxing does not make untrusted code harmless. Toolchains may contain their
own interpreters and host integrations, and a profile that auto-allows network
or arbitrary execution intentionally increases risk. Do not expose container
sockets, privilege escalation tools, or unrelated home directories.

Non-Linux platforms do not have the bwrap boundary in this release. Treat them
as requiring a separate VM/container security boundary for untrusted work.
If bwrap is unavailable, execution fails rather than silently falling back to
host execution unless the operator explicitly selects `unsafe` mode.

## Report a vulnerability privately

Before public release, the maintainer must enable **GitHub Private Vulnerability
Reporting** for this repository. Report through the private advisory form:

<https://github.com/GolovIaroslav/test/security/advisories/new>

Do not open a public issue with an exploit or live credential. Include the
affected version, a minimal reproduction, impact, and whether the issue escapes
the workspace, exposes a secret, bypasses an approval, or reaches the network.
