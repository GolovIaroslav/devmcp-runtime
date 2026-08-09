# Runtime security

Use Linux+bwrap for the beta security boundary. The runtime additionally
normalizes workspace-relative paths, rejects symlink escape, filters sensitive
environment values, uses argv execution with `shell=False` by default, and
keeps tokens outside the workspace.

Unsafe host mode is explicit, visibly reported as `SANDBOX: UNSAFE HOST MODE`,
and is not a silent fallback. Do not expose Docker/Podman sockets. Do not give
the model host privilege escalation or direct access to unrelated home files.

The local UI is loopback-only with strict Host/Origin checks, CSRF protection,
CSP, SameSite cookies, and secret redaction. Profiles and scoped approvals
control runtime capabilities; legacy permission modes are compatibility presets
only when no profile is active. See the authoritative [security policy](../SECURITY.md)
for residual risks and private reporting. Do not include live credentials in an
issue.
