# DevMCP autonomy and security architecture

This document describes the incremental autonomy architecture layered on top of
the existing DevMCP Runtime. It is intentionally not a rewrite: existing tools
remain available, legacy configuration remains accepted, and the host-security
floor is independent from user-facing convenience policy.

## Old permission model

Historically two models could both influence one command:

1. policy profiles (`safe`, `balanced`, `power`, `autonomous`, `custom`);
2. legacy `permission_mode` (`safe`, `trusted`, `dangerous`).

The profile might resolve a capability to AUTO while low-level legacy gates
still rejected shell expansion, inline scripts, network, sensitive environment,
or temp behavior. The result was hard to reason about and encouraged agents to
search for registered-task spellings instead of performing normal developer
operations.

## New permission model

At Runtime initialization there is one effective capability matrix. An explicit
policy profile is authoritative. When only a legacy permission mode is supplied,
it is converted once (`safe -> safe`, `trusted -> power`,
`dangerous -> autonomous`). After initialization command authorization does not
consult a second legacy permission matrix.

The matrix answers user-facing `auto` / `ask` / `deny`. It does **not** own the
host-security floor. The floor remains non-negotiable even in `autonomous` or a
legacy `dangerous` configuration.

## Immutable host-security floor

The following remain outside model-controlled policy escalation:

- no privilege escalation (`sudo`, `su`, `doas`);
- no direct host Docker/Podman control or model-visible container socket;
- no arbitrary host filesystem access;
- no protected credential/runtime paths;
- no model-supplied sandbox-attestation marker;
- no unrestricted inheritance of host credentials;
- no destination-scoped network promise unless the selected backend can enforce
  destination filtering;
- no silent sandbox downgrade when a required backend cannot initialize.

Bubblewrap uses an empty/private namespace layout, drops all capabilities, and
disables creation of additional user namespaces for model-controlled processes.
The filesystem view exposes only the selected snapshots plus a declarative,
audited read-only system/toolchain view. `/etc` is not mounted wholesale.

## Canonical filesystem authority

Filesystem trust is based on canonical containment rather than path spelling.
Absolute paths, relative paths, and paths containing `..` are treated the same
after canonicalization. A path is usable only when its canonical target lies in
the operation's authorized read/write roots and does not traverse a protected
credential/runtime path. Symlink escapes remain denied.

The selected project is always the primary root. `grant_root` (retired in v0.1.0b1) previously
added one existing directory under the operator-defined `DEVMCP_GRANTABLE_ROOTS` ceiling as an
in-memory capability lease. The grant never changed project discovery roots or survived restart.

Additional execution roots are copied through the same secret-filtered snapshot
pipeline as the primary project. The command sees those snapshots mounted at the
canonical original paths, which allows compiler/LSP absolute paths without
directly exposing the corresponding host sibling directory.

## Capability leases

`grant_capability` (retired in v0.1.0b1) previously provided bounded escalation
instead of requiring a global profile switch. Every lease had:

- opaque ID;
- owner logical context;
- exact capability and target/pattern;
- `once`, `task`, or `session` scope;
- expiration;
- optional opaque `task_scope_id`.

One-shot leases were consumed after the public operation that used them. A task
scope could span transport reconnects and was explicitly terminated with
`end_task_scope` (retired in v0.1.0b1); TTL was fallback cleanup. Leases were
memory-only and could not be made permanent by the model.

Ambient host secrets are always filtered. `sensitive_env_names` requests exact
host variable names and requires exact-name `env.sensitive` leases; unrelated
secrets remain absent.

## Execution hierarchy

The intended agent hierarchy is:

1. semantic/specialized tool where it adds real value;
2. registered task as a fast known-safe path;
3. `exec_argv` for normal arbitrary developer commands;
4. `exec_command` for shell-oriented workflows.

The task registry is therefore an optimization, not an allowlist of every
ecosystem command.

Shell syntax is a policy/risk signal, not the primary containment boundary.
Top-level segments are classified independently, while pipes, redirection,
heredocs, command substitution, conditionals, and ordinary inline scripts can be
used when the effective execution capability permits them. The OS sandbox/root
set/network backend contains their effects.

## Executor backends

The internal scheduler describes requirements before choosing a backend:

- `local_sandbox`: normal bubblewrap execution;
- `inherited_sandbox`: attested parent DevMCP namespace used for self-hosting;
- `isolated_worktree`: Git-isolated delegated/batch workflows;
- `ephemeral_container`: optional operator-managed container executor;
- `remote`: reserved extension point;
- `unsafe_host`: legacy explicit operator choice only, reported `secure=false`.

An ephemeral container is available only when the operator configures an
absolute trusted runner with `DEVMCP_EPHEMERAL_CONTAINER_RUNNER`. The runtime
rejects a runner located below any project-discovery or grantable root, as well
as a group/world-writable runner or runner directory, because that executable is
invoked host-side and must not be replaceable by model-authorized writes. The
runtime passes a bounded manifest containing runtime-owned filtered snapshots, canonical
mount destinations, action, cwd, sanitized environment, CPU/RAM/PID/time limits,
and controlled network targets. The model does not receive Docker/Podman socket
access. The container child environment uses container-private HOME, TMP, and XDG
paths. Before any extracted file output can be applied, the runner must return a
machine-readable enforcement attestation confirming filesystem isolation,
resource limits, network policy, private `/tmp`, and absence of a host container
socket. Missing/false attestation fails closed. A runner failure/protocol failure
is explicit, and extracted file output still passes DevMCP transaction/baseline
checks.

## Private temp and system view

In the secure local namespace `/tmp` is private writable tmpfs and
`TMPDIR`/`TMP`/`TEMP` point to it. This is normal developer scratch space, not a
dangerous write to host `/tmp`. It is discarded with the execution environment.

The read-only system view is declarative: OS metadata, dynamic-linker metadata,
normal toolchain runtime/config paths, and (only with network) DNS/CA metadata.
Credential stores and account secrets are excluded.

## Transactional mutating execution

Structured `exec_argv` defaults to transactional apply on the secure local
namespace backend. Before execution DevMCP hashes the secret-filtered snapshot,
including file modes. After exit 0 it computes the actual delta and authorizes
the concrete create/update/delete targets. Before replacing any authoritative
file it verifies that the authoritative bytes/mode still match the baseline
captured from the snapshot.

The atomic committer applies the complete staged set or rolls back the staged
set on installation failure. It never runs `git reset --hard`, never replaces a
dirty worktree with HEAD, and treats pre-existing uncommitted user content as the
baseline. A concurrent edit causes `TRANSACTION_CONFLICT`; the user's current
bytes are preserved. Non-zero exit/timeout discards snapshot mutations.

This is the central compensation for allowing normal mutating shell/developer
commands without treating command spelling as the security boundary.

## Compatibility impact

- Existing tool names remain available.
- `exec_command(cmd=...)` remains the compatibility shell surface and defaults
  to transaction discard.
- Legacy `exec_command(argv=...)` remains accepted; new callers should prefer
  `exec_argv`.
- Legacy `permission_mode` is accepted as startup configuration but is no longer
  a second runtime gate.
- Explicit `sandbox_backend=unsafe` remains available for operator-owned legacy
  fixtures/workflows and is reported as insecure; secure execution never falls
  back to it automatically.
- HTTP `context_id` / shared job handles remain the continuity model introduced
  previously.
- `active_project_file` remains initial-default-only for HTTP logical contexts.

## Feature flags and migration settings

- `DEVMCP_GRANTABLE_ROOTS`: operator ceiling for temporary additional roots.
  Default: empty. Project discovery roots are deliberately not grant authority.
- `DEVMCP_EPHEMERAL_CONTAINER_RUNNER`: absolute operator-owned executable
  implementing `devmcp-ephemeral-container-v1`. Unset means the backend is
  unavailable, not silently emulated.
- `DEVMCP_CONTAINER_CPU_LIMIT`: default `2`.
- `DEVMCP_CONTAINER_MEMORY_MB`: default `4096`.
- `DEVMCP_CONTAINER_PIDS_LIMIT`: default `512`.
- `DEVMCP_LOGICAL_CONTEXT_TTL_SECONDS`: logical-context retention.
- `DEVMCP_COMPLETED_JOB_TTL_SECONDS`: completed shared-job retention.

No migration flag enables host Docker/Podman socket access or removes the
security floor.

## Deliberately retained restrictions

- direct privilege escalation;
- direct host container-engine control;
- protected credential/runtime paths and ambient secret inheritance;
- filesystem access outside canonical authorized roots;
- symlink escape/write-through;
- arbitrary/permanent model-controlled permission escalation;
- domain/host network claims on a backend that cannot enforce them;
- transactional apply when the snapshot/backend cannot provide unambiguous
  rollback semantics;
- unbounded changed-file/output sizes.

These restrictions correspond to effects that the current enforcement layer
cannot safely roll back or contain. Normal developer syntax and ordinary
package/test/build tooling are not restricted merely because their command line
is unfamiliar.

## Diagnostics extension point

`code_diagnostics` is an optional normalization layer above filesystem/exec. The
provider registry currently includes a generic compiler/traceback text provider
and can accept future language-server/compiler adapters without making an IDE a
Runtime dependency. A normalized diagnostic path is still checked by the normal
root resolver and cannot create new authority.
