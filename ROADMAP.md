# Roadmap

## Beta

- Stabilize the MCP schema version and migration behavior.
- Expand tested task templates without expanding the tool catalog.
- Improve the persistent sandbox/session story for dev servers and Playwright.
- Add more UI diagnostics and service integration tests.

## After beta

- A well-tested rootless Podman backend where platform behavior is explicit.
- Additional MCP-client integration guides.
- More complete macOS and Windows backends only when their isolation claims
  can be tested honestly.

## Deferred design notes — do not implement in the current hardening pass

### Sandbox diff-back

- Problem: formatter or code generator changes the sandbox copy, but the
  authoritative workspace remains unchanged or receives an uncontrolled copy.
- Minimal design: expose a read-only sandbox diff, require an exact-baseline
  and policy-checked patch approval, then apply that patch through the existing
  atomic patch engine into the authoritative workspace.
- Security boundary: the sandbox never writes back implicitly; path, symlink,
  secret, size, and approval checks remain authoritative at the diff-back step.
- Acceptance tests: sandbox-only changes are visible in a bounded diff; an
  approved clean diff applies atomically; baseline drift, traversal, symlink,
  secret, and oversized diffs are rejected without partial writes.

### Persistent/shared sandbox sessions

- Problem: a dev server started by one operation exits before Playwright or a
  later MCP call can reach it.
- Minimal design: add an explicitly named session lease that retains one
  sandbox process group and its private temp filesystem until idle timeout or
  explicit close.
- Security boundary: the lease keeps the same workspace allowlist, network
  policy, authentication, output limits, and process ownership; it does not
  turn host `/tmp` or arbitrary host sockets into mounts.
- Acceptance tests: a server survives across two calls, Playwright can reach
  only the intended listener, idle/explicit close removes processes and temp
  data, and cross-session access is denied.

### Multi-chat / multi-workspace concurrency

- Problem: two ChatGPT conversations can race while writing the same
  authoritative checkout.
- Minimal design: acquire a workspace-scoped lock with owner/session metadata,
  expose read-only status, and require an explicit handoff or conflict retry
  for writes.
- Security boundary: locks are advisory only in the UI but mandatory in the
  patch/approval commit path; stale ownership is expired safely and never grants
  access to a different workspace.
- Acceptance tests: concurrent reads work, concurrent writes serialize, a
  stale owner cannot commit, and a second workspace is never blocked by the
  first.

### Per-session Git worktrees

- Problem: a session needs an isolated branch/worktree so its patch and tests do
  not collide with another session or the maintainer's checkout.
- Minimal design: create a temporary worktree and branch per MCP session, route
  all workspace tools to it, then offer an explicit review/apply/discard flow.
- Security boundary: worktree creation/removal is bounded to a configured Git
  repository, never follows an untrusted path, and cannot replace the
  authoritative checkout without a reviewed diff-back operation.
- Acceptance tests: two sessions get distinct worktrees and branches, commits
  remain isolated, cleanup removes the temporary worktree, and apply/discard
  handles dirty or deleted authoritative files safely.

### Optional desktop notification for pending approvals

- Problem: a local approval can wait unseen while ChatGPT is blocked on a
  pending operation.
- Minimal design: add an opt-in notification adapter that emits only a generic
  pending-approval event and links to the loopback UI.
- Security boundary: notifications contain no command, path, token, or secret;
  the adapter is disabled by default, loopback-only, and cannot approve work.
- Acceptance tests: opt-in notifications fire once per pending approval,
  repeated polling does not spam, opt-out emits nothing, and notification
  payloads contain no sensitive values.
