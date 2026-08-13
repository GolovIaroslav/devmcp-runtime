# DevMCP execution security / permission surface audit

<!--
AUDIT_STATUS: COMPLETE
AUDITED_REPOSITORY: GolovIaroslav/devmcp-runtime
AUDITED_BASE: 938f7398879c7f24a35a40f72a3dc153f60ca1e5
AUDIT_BRANCH: audit/execution-security-surface
AUDIT_DATE: 2026-08-13
TARGET_THREAT_MODEL: personal-development-machine-danger-full-access
-->

## Scope and decision rule

This document is the source-backed inventory for removing legacy execution-policy code without repeating the investigation. It audits `main` at `938f7398879c7f24a35a40f72a3dc153f60ca1e5`.

Target threat model:

- DevMCP runs on a personal development machine under the current OS user.
- `dangerous` / `full-access` is an explicit operator choice.
- In full-access, DevMCP must not add filesystem, command, environment, network, or approval restrictions on top of what the current OS user can do.
- DevMCP must not create new OS privilege. Running `sudo`, `su`, `doas`, Docker, Podman, setuid executables, or arbitrary host paths is acceptable only to the extent the current OS user/OS configuration already permits it.
- Authentication of remote callers, process lifetime control, timeouts, output bounds, Git concurrency/CAS, and state integrity are **not local execution sandboxes**. They remain boundaries even in full-access.

### Protection taxonomy

| Type | Meaning | Full-access disposition |
|---|---|---|
| `LOCAL_POLICY` | In-process allow/ask/deny, path, command, env, network, capability or approval policy | Remove/bypass from full-access |
| `LOCAL_SANDBOX` | OS isolation or copied execution workspace | Remove/bypass from normal full-access; retain only explicit sandbox/compatibility features |
| `TOOL_CONTRACT` | Semantic validation needed for a structured tool to mean what its API says | Keep where it is not an authority restriction; split from security policy |
| `PROTOCOL_BOUNDARY` | Authentication/origin/caller boundary | Keep |
| `RELIABILITY_BOUNDARY` | Process lifecycle, timeout, memory/output limits | Keep |
| `STATE_INTEGRITY` | CAS, drift detection, writer serialization, remote-head verification | Keep |

## Executive result

1. **The current model has two real permission axes.** `policy_profile` controls capability decisions, but `permission_mode` still controls default `execution_mode`, whether `full-access` may be requested, whether the fast path is used, whether Landlock is considered, and whether `dangerously_skip_all_permissions` is true. An explicit `policy_profile="autonomous"` therefore does not make legacy `permission_mode` irrelevant.
2. **Current `dangerous/full-access` is not actually unrestricted.** The legacy fast path still rejects `sudo`, `su`, and `doas`. Explicit-profile execution is stricter: `_profile_authorize_command()` also rejects Docker/Podman sockets, `docker`, `podman`, `nsenter`, `bwrap`/`bubblewrap`, setuid/setgid executables, command paths outside authorized roots, and sensitive paths.
3. **`dangerous` is full-access for shell only, not for structured file tools.** The current regression suite explicitly encodes this in `test_legacy_dangerous_mode_is_full_access_for_shell_only`: shell can `cat .env`, while `read_file(.env)` is rejected. That conflicts with the target threat model.
4. **One legacy command classifier is dead.** `ApprovalEngine.evaluate_command()` has no runtime call site. Repository-wide search finds only the method definition and the stale claim in `ARCHITECTURE.md` that exec handlers call it.
5. **The actual runtime command classifiers are heuristics, not containment boundaries.** `_command_domain_capabilities()`, `_network_capability()`, `_profile_command_capabilities()` and command-path token scanning affect policy/UX but cannot prove what arbitrary shell code will do.
6. **bwrap/Landlock/snapshots are already absent from the normal legacy dangerous fast path.** They remain mainly in explicit-profile/compatibility or explicit transaction execution. They should not be reintroduced into normal full-access.
7. **The measured full-access no-op cost is not primarily policy lookup.** Legacy dangerous exec performs zero `_policy_decision_for_capabilities()` calls per operation, yet `argv=["true"]` is ~386 ms median on the audit host. Explicit autonomous was ~386 ms as well. The dominant no-op cost is common process/session execution machinery, not the capability matrix. Removing policy layers alone should not be sold as a ~hundreds-x exec-speed fix.
8. **Protocol/reliability/state layers must survive simplification.** MCP/tunnel auth, Origin validation, process-group cleanup, watchdog timeout, output bounds, `AtomicPatchCommitter` baseline CAS/rollback, state drift checks, remote-ref verification, and writer leases protect different failure classes from local command sandboxing.

## Effective-state conflict: `permission_mode` + `policy_profile`

### Source trace

- `coding_tools_mcp/policy.py::legacy_profile()` maps `safe -> safe`, `trusted -> power`, `dangerous -> autonomous`.
- `coding_tools_mcp/server.py::Runtime.__init__()` sets `_explicit_policy_profile`, `policy_profile`, `effective_capability_rules`, `_profile_managed`, and `dangerously_skip_all_permissions`.
- `coding_tools_mcp/server.py::Runtime._profile_exec_command()` derives default `execution_mode` from **`permission_mode`**, even when an explicit profile is active.
- `_profile_exec_command()` rejects `execution_mode="full-access"` unless `permission_mode == "dangerous"`, and rejects `workspace-write` when `permission_mode == "safe"`.
- `Runtime.exec_command()` uses the fast path only when `not self._profile_managed`.
- `Runtime.landlock_enabled()` is tied to profile-managed execution.

### Concrete misleading states

| Inputs | Capability state | Execution state | Why misleading |
|---|---|---|---|
| `permission_mode=safe`, `policy_profile=autonomous` | all profile capabilities `auto` | default `read-only`; explicit `full-access` forbidden | profile appears authoritative but mode still caps execution |
| `permission_mode=trusted`, `policy_profile=autonomous` | all profile capabilities `auto` | default `workspace-write`; explicit `full-access` forbidden | same conflict |
| `permission_mode=dangerous`, no explicit profile | legacy adapter reports `autonomous` | `full-access`, fast path, skip flag true | closest to target model, but still blocks sudo/su/doas and structured sensitive/outside-root file access |
| `permission_mode=dangerous`, `policy_profile=autonomous` | all `auto` | `full-access`, but profile-managed slow/compat path | nominally same authority, materially different execution stack |

Relevant tests currently prove capability-profile authority but do not prove execution-mode authority:

- `tests/compliance/test_autonomy_architecture.py::AutonomyArchitectureTests.test_explicit_profile_is_authoritative_over_legacy_permission_mode`
- `tests/test_release_prep.py::ReleasePrepTests.test_active_profile_controls_exec_and_network_not_legacy_safe_mode`
- counter-evidence is encoded by `tests/compliance/test_runtime_helpers.py::RuntimeHelperTests.test_execution_mode_cannot_escalate_legacy_permission_mode`.

**Decision:** establish one canonical effective execution authority. `permission_mode` may remain as a legacy input adapter during compatibility, but must not be a second post-resolution authority axis. For the target design, resolved `full-access` must have an early, obvious bypass of all `LOCAL_POLICY` and `LOCAL_SANDBOX` checks while preserving protocol/reliability/state layers.

## Measurements on audited main

Method: in-process `.venv/bin/python` microbenchmarks against source at the audited SHA, using a temporary one-file workspace. Exec benchmarks used `sandbox_backend="unsafe"` so they measure DevMCP process/session plumbing rather than bwrap. Numbers are local-host measurements, not portable performance claims.

| Operation | Configuration | N | Median | p95 | Policy decisions / operation |
|---|---:|---:|---:|---:|---:|
| no-op argv `true` | legacy `dangerous`, full-access | 25 | 386.337 ms | 404.292 ms | 0 |
| shell `true` | legacy `dangerous`, full-access | 25 | 400.067 ms | 404.827 ms | 0 |
| shell setup delta | shell median - argv median | — | +13.730 ms | — | — |
| `read_file(f.txt)` direct method | legacy `dangerous` | 100 | 0.194 ms | 0.311 ms | 0 observed |
| `call_tool("read_file")` | legacy `dangerous` | 100 | 0.323 ms | 0.518 ms | 0 observed |
| read tool-wrapper delta | call_tool - direct | — | +0.129 ms | — | — |
| patch one-line replacement, `dry_run=true`, direct | legacy `dangerous` | 50 | 0.408 ms | 0.666 ms | 0 |
| patch one-line replacement, `dry_run=true`, `call_tool` | legacy `dangerous` | 50 | 0.575 ms | 0.843 ms | 0 |
| patch tool-wrapper delta | call_tool - direct | — | +0.167 ms | — | — |
| no-op argv `true` | explicit `autonomous` + dangerous | 7 | 386.382 ms | — | 1 (`exec.arbitrary`) |
| no-op argv `true` | legacy `dangerous` | 7 | 392.945 ms | — | 0 |
| no-op argv `true` | legacy `trusted` | 7 | 389.637 ms | — | 0 |

Dynamic policy-decision instrumentation for explicit `autonomous` observed:

- exec `true`: 1 decision, capabilities `{"exec.arbitrary"}`;
- read `f.txt`: 0 decisions;
- one-file small patch dry-run: 1 decision, `{"workspace.patch_small"}`.

`Runtime.__init__()` separately queries two network capabilities to populate `allow_network`; those are startup decisions, not per-operation decisions.

The ~390 ms no-op is consistent across policy arrangements and therefore remains a **separate performance investigation in process/session machinery**. Do not delete timeout/process cleanup merely because that path is expensive; profile it independently.

# A. BROKEN

These layers either have no runtime enforcement or present an effective-state guarantee contradicted by another active path.

| ID | NAME | SOURCE PATH / FUNCTION | ACTIVE MODES | ACTUAL GUARANTEE | ACTUALLY ENFORCED? | BYPASSES / COUNTEREXAMPLE | DUPLICATES | PER-COMMAND COST | TOOL-CALL / UX COST | TESTS DEPENDING ON IT | FAILURE HISTORY | DECISION |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `A01` | Dead legacy command classifier | `coding_tools_mcp/approval.py::ApprovalEngine.evaluate_command`; stale caller description in `ARCHITECTURE.md` | none in current runtime | Claims textual `ALLOW/ASK/DENY` for sudo/doas/su/mount/docker/rm etc. | **No.** Repository search has no runtime call site. | Every current exec path bypasses it because nothing calls it. | Real classifiers in `server.py` | 0 | Misleads maintainers; stale architecture docs | no direct runtime dependency located | Left behind while command policy moved into capability/profile code | **REMOVE** method and stale docs after confirming no external import API contract |
| `A02` | “Explicit profile is authoritative over legacy permission mode” effective-state claim | `Runtime.__init__`, `_profile_exec_command`, `landlock_enabled`, `exec_command` | explicit profiles | Capability matrix follows profile. | **Partially only.** Execution mode and full-access eligibility still follow `permission_mode`; fast-path selection also differs. | `permission_mode=safe, policy_profile=autonomous` cannot request full-access. | legacy mode matrix + profile matrix | extra branching; profile path adds one policy decision for typical exec | confusing UI/config; can require changing two knobs for intended authority | `test_explicit_profile_is_authoritative_over_legacy_permission_mode`; `test_execution_mode_cannot_escalate_legacy_permission_mode`; `test_active_profile_controls_exec_and_network_not_legacy_safe_mode` | commits `fadef9d` (accept autonomous profile), `13d4ffd` (align legacy policy tests) show continuing compatibility friction | **REMOVE dual authority**. Resolve once to one effective mode; retain old fields as input aliases only during compatibility |

# B. REDUNDANT / OVERENGINEERED FOR TARGET FULL-ACCESS

These layers may enforce something in safe/trusted/profile-managed execution, but they are unnecessary or contrary to the target full-access threat model. “REMOVE” below means remove from full-access; some rows are explicitly retained as compatibility or opt-in features.

| ID | NAME | SOURCE PATH / FUNCTION | ACTIVE MODES | ACTUAL GUARANTEE | ACTUALLY ENFORCED? | WHAT BYPASSES IT? | DUPLICATES / OVERLAP | PER-COMMAND COST | TOOL-CALL / UX COST | TESTS DEPENDING ON IT | FAILURE HISTORY | DECISION |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `B01` | Legacy `permission_mode` capability matrix | `server.py::PERMISSION_MODE_CAPABILITIES`, `runtime_policy_from_args`, `Runtime.__init__` | safe/trusted/dangerous | Maps mode to network/shell/Landlock/env/tmp/skip booleans and also maps to profile rules. | Yes. | Explicit profile replaces capability rules but not execution authority. | `policy_profile` capability matrix | startup + branches; no measured per-command policy lookup in legacy dangerous | second user-facing permission concept | `test_permission_modes_apply_expected_gates`, autonomy architecture legacy-adapter tests | `13d4ffd`, `19d5758` | **COMPATIBILITY-ONLY** input adapter. Do not let it remain an independent authority after resolution |
| `B02` | Capability profile matrix | `coding_tools_mcp/policy.py::{CAPABILITIES, profile_rules, effective_rules}`; `Runtime._policy_decision_for_capabilities` | explicit profiles; legacy rules also materialized for status | Per-capability `auto/ask/deny`. | Yes for profile-managed paths; not the normal legacy dangerous fast path. | `not _profile_managed` fast execution; dangerous shell bypasses profile authorization. | permission mode + command/path gates | typical explicit-profile exec: 1 lookup; small patch: 1/file; rules are in-memory dict reads | approvals / denies; configuration surface | `test_policy_profiles_cover_each_capability_and_custom_is_data`, `test_permission_profile_matrix_and_legacy_adapter_are_deterministic` | policy/config fixes `fadef9d`, `f6ddfd`, `13d4ffd` | **REMOVE from full-access; COMPATIBILITY-ONLY** for safer modes if retained |
| `B03` | Approval engine + approval IDs | `approval.py::{Operation, ApprovalEngine.request_approval, approve, deny, consume}`; `server.py::_profile_authorize_command`, `apply_patch`, Git/tool approval paths | ask decisions / high-risk patch / selected Git ops | Durable SQLite approval with operation digest, expiry, one-shot consume. | Yes where invoked. | auto policy, legacy dangerous fast exec, any path not wired to approval engine. | capability matrix and patch risk classification | zero on auto; SQLite/open/hash on request/consume | **one or more extra user/tool round trips**; approval ID must be replayed | `test_audit_v4_security` approval lifecycle; `test_live_dogfood_v5`; `test_release_prep` | no core correctness failure found; UX complexity grew with profile system | **REMOVE from full-access; COMPATIBILITY-ONLY** for ask-based modes |
| `B04` | Capability leases | `session_state.py::CapabilityLeaseRegistry`; `server.py::{_matching_capability_lease,_leased_command_capabilities,grant_capability,list_capability_leases,revoke_capability_lease}` | profile-managed / grant APIs | Owner/context/scope/TTL limited temporary capabilities. | Yes for profile authorization and granted roots/caps. | full-access legacy fast path; capabilities already `auto`; arbitrary shell can use OS authority. | approval IDs + capability matrix | matching scans in-memory leases for relevant caps/targets | grant call(s), lease IDs, expiry/scope management | `test_autonomy_architecture` capability lease tests; `test_http_session_state` | no failure located | **REMOVE from full-access; COMPATIBILITY-ONLY** |
| `B05` | Task scopes for grants | `session_state.py` task-scoped leases; `server.py::{_active_task_scope_id,end_task_scope}` | task-scoped leases | Revokes task capability/root authority at scope end. | Yes. | session/full-access paths without leases. | capability leases | small scope/lease lookup | caller must propagate opaque `task_scope_id` across tool calls | `test_task_scoped_root_grant_is_portable_and_revoked_on_scope_end` | no failure located | **REMOVE from full-access; COMPATIBILITY-ONLY** |
| `B06` | `grant_root` + grantable-root ceiling | `server.py::{grant_root,readable_roots,writable_roots}`; constructor `grantable_roots` | profile/grant workflows and structured file tools | Additional roots must be canonical, inside operator `grantable_roots`, and not overly broad ancestors; creates read/write lease. | Yes. | legacy full-access shell can already touch OS-visible paths; structured tools still cannot. | workspace authority + root leases | root canonicalization/lease lookup when used | explicit grant round trip; causes shell/file-tool authority mismatch | `test_autonomy_architecture` root grant tests; `test_http_session_state` grant persistence/isolation | no direct failure located | **REMOVE as security authority in full-access.** Preserve project/root metadata only as tool context; keep grants only for compatibility modes |
| `B07` | Workspace/project authority containment | `server.py::Workspace.resolve_existing_at`, `resolve_for_write_at`; `Runtime.resolve_existing`, `resolve_for_write`; `readable_roots/writable_roots` | structured file/search/patch tools; profile command path checks | Reject paths outside selected/granted roots; rejects symlink escapes. | Yes. | legacy dangerous shell bypasses it; `read_file`/patch do not. | grant roots + sensitive checks | path `resolve/stat/relative_to` per file/path | can require project selection/root grant; creates inconsistent “full-access shell only” semantics | `test_security::test_path_traversal_absolute_paths_and_symlink_escape_are_rejected`; `test_tool_golden::test_apply_patch_rejects_absolute_traversal_and_symlink_escape`; runtime-helper multi-root tests | symlink/path hardening evolved repeatedly; no current bypass proven for structured paths | **REMOVE authority rejection in full-access structured tools. KEEP canonicalization/validation as TOOL_CONTRACT** |
| `B08` | Sensitive-path deny | `coding_tools_mcp/path_security.py::{sensitive_raw_path_reason,sensitive_path_reason}`; workspace resolvers; delegated-agent guards | structured tools and profile-managed command scanning | Blocks `.git`, `.ssh`, cloud creds, `.env*`, private-key extensions and selected config trees. | Yes on structured paths/profile command scans. | legacy dangerous shell can access them. | workspace containment + env filtering | path-component checks per path | surprising: `dangerous` shell can `cat .env`, `read_file` cannot | `test_legacy_dangerous_mode_is_full_access_for_shell_only`; security/path tests; delegate sensitive-path tests | sensitive-path hardening has repeatedly changed; no single stable boundary | **REMOVE from full-access. COMPATIBILITY-ONLY** elsewhere |
| `B09` | Runtime shell capability classifiers | `server.py::{_command_domain_capabilities,_network_capability,_profile_command_capabilities,_leased_command_capabilities}` and `NETWORK_RE` | explicit profile exec | Guesses deps/db/git/network/env/exec capabilities from command text/args. | It enforces policy decisions on the guessed classification, **not command semantics**. | shell indirection, interpreters, generated scripts, alternate clients/tools; legacy fast path skips it. | profile matrix + network/env gates | regex/shlex/token scans + one policy decision; measured decision count 1 for `true` | can trigger approval based on textual classification | release-prep capability matrix tests; live dogfood approval tests | Hermes upstream security model independently classifies in-process scanners as heuristics, not containment; DevMCP has had classifier adjustments | **REMOVE from full-access. If compatibility mode retains it, document as heuristic, never boundary** |
| `B10` | Hardcoded privilege/container/setuid command deny | `server.py::{_contains_always_denied_command,_reject_setuid_executable,_check_command_paths}` plus full-access `sudo/su/doas` scan in `_execute_command_legacy` | explicit profiles; legacy full-access has smaller sudo/su/doas floor | Prevents selected privilege/container/nested-sandbox strings/executables even when OS user can run them. | Yes for matched syntax. | alternate invocation/indirection can evade textual gate; legacy dangerous bypasses Docker/Podman gate but not sudo/su/doas. | command classifier + sandbox assumptions | shlex/token/path/stat scans; full-access token scan | hard deny, no approval escape | `test_full_access_still_blocks_privilege_escalation`; `test_autonomous_profile_runs_arbitrary_exec_without_approval_but_not_sudo`; nested sandbox tests | `308fdc` added nested sandbox escape denies | **REMOVE from full-access.** DevMCP must not grant privilege, but must let OS decide whether current user may invoke these tools |
| `B11` | Network capability gate / textual detection | `NETWORK_RE`, `_network_capability`, `_profile_command_capabilities`, `ExecutionRequirements.network`, bwrap `allow_network` | read-only/profile modes; workspace-write/full-access currently host-network by execution mode | Coarse network off/on with bwrap; textual classifier decides capability. | Coarse namespace gate is real when bwrap network is off; text inference is heuristic. | workspace-write/full-access host network; non-obvious network code can evade text inference. | profile capability matrix | regex + requirement selection; bwrap namespace cost only in sandbox modes | may ask for network approval | `test_network_script_reaches_real_bwrap_and_is_isolated`; policy network tests | network/bwrap CI regressions in `19d5758` | **REMOVE network restrictions from full-access. COMPATIBILITY-ONLY** coarse network sandbox for safer modes |
| `B12` | Network-target leases / target-filter executor requirement | `_leased_command_capabilities`, `_profile_exec_command`; `executors.py::ExecutionRequirements.network_targets`; ephemeral container manifest | explicit profile when `network_targets` supplied | Requires a backend that claims enforceable target filtering; local bwrap does not claim it. | Yes by executor selection; actual target enforcement is delegated to trusted runner attestation. | no targets -> only coarse network; full-access fast path does not use this. | capability leases + container executor | executor selection + runner IPC only when requested | extra grants/runner configuration; may fail `CAPABILITY_UNAVAILABLE` | `test_autonomy_architecture` ephemeral-container target tests | no failure located | **COMPATIBILITY-ONLY / MOVE OUT OF FULL-ACCESS FAST PATH** |
| `B13` | Shell environment filtering / sensitive env leases | `server.py::{_command_env,_base_command_env,is_sensitive_env_name,is_ecosystem_cache_env_name}`; shell-env policy | safe/trusted/profile execution; full-access direct path inherits host env | Removes credential/cache env, optionally re-adds leased sensitive names; pins private HOME/TMP for sandbox. | Yes outside direct full-access. | direct legacy full-access intentionally inherits ambient env. | env.sensitive capability + sandbox private dirs | O(number of env vars) copies/filtering per command | hidden auth/toolchain differences; grants needed for selected secrets | environment/runtime helper tests; MSVC compatibility tests | `a358187` added shell environment inheritance/MSVC support after environment isolation caused toolchain friction | **Full-access: host env pass-through. COMPATIBILITY-ONLY** filtering for sandbox modes |
| `B14` | Dedicated Git environment filtering | `server.py::_git_env` and state mutation MRO override path | structured Git tools | Strips ambient credentials/hooks and supplies controlled Git credentials/config. | Yes for Git wrapper subprocesses. | full-access shell `git` uses host env; dedicated wrapper may behave differently. | shell env filtering | env construction per Git command | can make structured Git auth differ from shell | state/Git helper tests | `b18eb27` fixed `_git_env` MRO recursion in state mutation wiring | **Do not mix with exec sandbox. Review separately.** For strict target parity, full-access Git wrappers should not be less capable than current-user Git unless the tool contract explicitly documents credential isolation |
| `B15` | bubblewrap local sandbox | `sandbox.py::ExecutionSandbox.get_bwrap_args`; `server.py::_execute_command_legacy` | read-only/workspace-write and profile/compat paths when backend bwrap | OS mount/process/network namespace isolation, cap drop, private `/tmp`, root mounts RO/RW. | Yes when bwrap successfully starts. | legacy dangerous/full-access sets unsafe/direct host; inherited/container backends are separate. | Landlock + snapshots + path policy | process startup/mount namespace cost; not present in audited full-access fast path | sandbox failures can block command entirely | `test_audit_v4_security`, `test_toolchain_system_view`, `test_bwrap_has_private_writable_tmp...` | `19d5758` sandbox CI/AppArmor fixes; `13d4ffd` toolchain visibility; `b0b9a7a` private env; benchmark JDK read-root friction | **REMOVE from normal full-access; KEEP explicit sandbox/safer-mode compatibility only** |
| `B16` | Landlock defense-in-depth wrapper | `server.py::{landlock_enabled,open_landlock_ruleset,landlock_*}` and `scripts/landlock_exec.py` | explicit profile compatibility path when secure backend/conditions allow; normal legacy execution explicitly avoids initialization | Additional filesystem access restriction. | Yes when initialized; fail/open warning behavior exists for unavailability. | normal legacy safe/trusted test asserts no Landlock initialization; dangerous full-access does not use it. | bwrap + root/path restrictions | ruleset setup + wrapper process/FD; absent from normal fast path | can produce diagnostics/warnings and toolchain read failures | `test_exec_command_warns_and_runs_when_landlock_is_unavailable`; `test_exec_command_uses_landlock_wrapper_without_preexec_fn`; `test_normal_exec_does_not_initialize_landlock` | benchmark: JDK required extra read roots; Rust failure was falsely diagnosed `LANDLOCK_READ_ROOT_BLOCKED`; `65cff44` removed a dead in-process Landlock variant | **REMOVE from normal full-access; COMPATIBILITY-ONLY** |
| `B17` | Inherited sandbox detection / attestation | `sandbox.py::{inherited_sandbox_backend,legacy_devmcp_parent_sandbox_backend,detect_sandbox_backend}` | nested/self-host sandbox selection | Kernel evidence for constrained uid map, zero effective caps, private `/tmp`; weaker legacy self-host heuristic separately gated. | Strong path is evidence-based; legacy heuristic is not equivalent. | explicit unsafe host/full-access; no inherited marker/evidence. | bwrap backend selection | startup/procfs checks, not per command | can change selected backend or reject nesting | `test_inherited_sandbox_requires_dropped_caps_and_private_tmp`; backend selection tests | nested bwrap/AppArmor history; legacy fallback exists because nesting was operationally problematic | **MOVE OUT OF FULL-ACCESS FAST PATH. KEEP only for explicit sandbox/self-host detection** |
| `B18` | Executor registry / backend scheduler | `executors.py::{ExecutionRequirements,ExecutorBackend,ExecutorRegistry.select}`; `_profile_exec_command` | profile-managed execution | Selects backend by declared roots/network/transaction/TTY/targets and secure/configured flags. | Yes for profile path. | legacy fast path selects read-only/workspace/unsafe execution directly. | policy profile + sandbox backend | object checks per profile command; runner setup for container | can fail because a compatible backend is unavailable | autonomy executor tests | no scheduler correctness failure located; architecture grew around sandbox requirements | **REMOVE scheduler from normal full-access; KEEP compatibility/explicit executor feature** |
| `B19` | Ephemeral-container trusted-runner validation + enforcement attestation | `executors.py::_validate_runner/reject_runner_below`; `server.py::_execute_ephemeral_container` required `enforcement` fields | explicitly configured container executor/profile path | Runner path ownership/mode checks; manifest requires filesystem/resource/network/private-tmp/no-socket enforcement and result attestation. | DevMCP verifies runner identity properties and runner’s returned attestations; actual isolation is external. | not selected in full-access fast path; malicious trusted runner can lie. | executor scheduler + snapshots | high: snapshot + runner process + JSON IPC | requires operator runner configuration; no TTY | `test_autonomy_architecture` runner validation/attestation tests | no failure located | **KEEP as explicit external executor feature, not as full-access policy** |
| `B20` | Execution workspace snapshots | `sandbox.py::ExecutionSandbox.create/_sync`; server additional execution sandboxes | explicit-profile/compat paths and explicit transaction/container paths | Copies filtered workspace into owned temporary directory before execution. | Yes when create path used. | normal nontransactional legacy execution uses authoritative workspace/direct path. | bwrap/Landlock/transaction | O(workspace files/bytes); potentially dominant on large trees | stale/copy semantics; missing filtered/symlink/toolchain content can surprise | snapshot cleanup and authoritative-workspace tests | `a18b2bc` hardened temp/project baselines; benchmark sandbox dependency/toolchain friction | **REMOVE from normal full-access. KEEP only explicit transaction/external-executor feature** |
| `B21` | Transaction snapshot/apply | `server.py::_execute_command_legacy` `transaction_mode="apply"`; `ExecutionTransaction`; `AtomicPatchCommitter` application | explicit `transaction_mode=apply` | Runs against snapshot, inspects changes, checks Git HEAD/branch, then applies staged result atomically; preserves pre-existing dirty work. | Yes. | default normal execution is direct/nontransactional despite legacy arg name `discard`. | snapshots + patch CAS + state checks | high: snapshot, scan/diff, Git checks | no streaming TTY; bounded semantics; explicit opt-in | transaction tests in `test_runtime_helpers` and `test_toolchain_system_view` | transaction conflict handling added to protect WIP | **KEEP explicit feature; MOVE OUT OF/KEEP OUT OF fast path.** Consider renaming default `discard` semantics because direct mode does not create/discard a snapshot |
| `B22` | Patch-risk classifier / destructive confirmation | `server.py::_analyze_patch`, `apply_patch`; thresholds `max_removed_lines/max_removed_percent`; `workspace.patch_small`, `workspace.patch_destructive`, `workspace.delete` | structured patch tool | Computes removed existing lines/%; converts destructive/create/delete/move into ask/deny/auto and ApprovalEngine flow. | Yes. | legacy dangerous still asks for delete/destructive patch; shell can edit/delete directly. | capability matrix + ApprovalEngine | O(diff lines/files); measured small dry-run 0.408 ms direct | approval round trip for destructive operations | `tests/compliance/test_patch_safety.py`; release-prep approval replay tests | `85bb225` restored imported patch thresholds | **REMOVE confirmation policy from full-access. KEEP parsing/diff/CAS/rollback** |
| `B23` | Policy activation / restart surface | `server.py::activate_policy_profile`; config policy persistence/restart | profile feature | Mutates configured profile and schedules/requires runtime restart. | Yes. | irrelevant when full-access is single resolved mode. | dual-axis configuration | not per command | extra configuration mutation/restart workflow | `test_autonomous_profile_can_activate_policy_profile_and_schedule_restart`; no-op activation test | `f6ddfd` introduced first-class activation | **COMPATIBILITY-ONLY** if profiles remain; delete after canonical mode migration if unused |
| `B24` | Fake read-only MCP annotations | runtime/config `fake_readonly_annotations` / tools-list annotation override | dangerous only when explicitly enabled | Changes client-visible annotations, not actual tool behavior. | Enforced only as metadata override. | server_info retains real catalog annotations; runtime actions unchanged. | client-side approval UX | none per command | can suppress client-side prompts by deliberately lying about mutability | runtime-helper annotation tests | `ef58775` restored it as fenced compatibility escape hatch after old profile removal | **COMPATIBILITY-ONLY; keep isolated from execution authority. Do not treat as security** |

# C. MINIMUM REQUIRED / NON-SANDBOX BOUNDARIES

These are not justification for restricting full-access execution. They protect caller identity, protocol correctness, process lifetime, data integrity, or concurrency and should remain.

| ID | NAME | SOURCE PATH / FUNCTION | ACTIVE MODES | ACTUAL GUARANTEE | ACTUALLY ENFORCED? | WHAT BYPASSES IT? | DUPLICATES / OVERLAP | PER-COMMAND COST | TOOL-CALL / UX COST | TESTS DEPENDING ON IT | FAILURE HISTORY | DECISION |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `C01` | Project/workspace identity (semantic, not authority) | project selection/runtime constructor, `Workspace.root`, selected project context | all | Establishes default cwd/repository/tool context. | Yes. | explicit cwd/path-capable tools may target another location only if API allows. | none | trivial | project selection call when changing context | project/session tests | recent state/runtime routing work | **KEEP**, but do not use identity as a full-access security ceiling |
| `C02` | Canonical path normalization as correctness primitive | `Workspace._canonical_roots`, `resolve_existing_at`, `resolve_for_write_at`, `Path.resolve`/relative display logic | structured path tools | Produces a canonical target and prevents ambiguous/symlink-racy interpretation within a structured operation. | Yes. | shell naturally has OS path semantics. | currently entangled with workspace/sensitive deny | filesystem stat/resolve | none if it does not deny authorized full-access paths | path/symlink tests | extensive hardening history | **KEEP/SPLIT** normalization from authority. In full-access, canonicalization may validate existence/type but must not reject merely for being outside workspace/sensitive |
| `C03` | Patch baseline CAS + atomic commit/rollback | `patching.py::{FileBaseline,StagedFile,AtomicPatchCommitter.commit,_assert_baseline,_rollback}` | all structured patch commits | Detects concurrent file changes before replacement; atomic per-file replace; full-set rollback or preserved recovery backups. | Yes. | raw shell edits intentionally bypass structured patch transaction guarantees. | state drift protects broader repo state; this is file-level CAS | hashes/file reads/fsync/rename proportional to patch | conflict requires reread/regenerate, not approval | `test_runtime_helpers` PATCH_CONFLICT/rollback tests | patch baseline limits/thresholds have been repaired; no reason to remove CAS | **KEEP** — state integrity, not security sandbox |
| `C04` | MCP HTTP bearer/OAuth authentication | `server.py::MCPHandler.is_authorized`; bearer `hmac.compare_digest`; OAuth token validation/config | HTTP server when auth configured; non-loopback startup requires auth unless explicit noauth | Prevents unauthenticated remote caller from invoking MCP tools. | Yes. | explicit noauth configuration / loopback operational choices. | tunnel may add another caller boundary | one token compare or OAuth validation per request | client must provide credential | `test_mcp_contract::test_bearer_auth_rejects_missing_or_wrong_token_and_accepts_valid_token`; OAuth tests | tunnel/auth fixes exist historically | **KEEP** — protocol boundary |
| `C05` | HTTP Origin validation | `server.py::is_allowed_origin`; `MCPHandler` GET/POST/OPTIONS checks | HTTP MCP | Accepts only http/https origins with clean authority; loopback or configured origin allowlist. | Yes when Origin present. | clients without browser Origin header follow auth boundary; configured allowed origins. | complements auth; does not replace it | URL parse + allowlist lookup per HTTP request | browser callers need allowed origin | `test_mcp_contract` Origin denied test | no current failure located | **KEEP** — browser/protocol boundary |
| `C06` | Tunnel authentication chain | `scripts/tunnel-common.sh`; `apps/devmcp/cli.py` tunnel-client setup | tunnel mode | Uses control-plane credential and MCP bearer token; secrets passed via restricted files where applicable; local MCP binds loopback. | Yes in secure tunnel path; explicit noauth remains possible with warnings. | operator can explicitly choose noauth/local alternatives. | MCP HTTP auth is inner boundary; control plane is separate | not per local command; transport overhead only | credential/setup requirement | tunnel scripts/CLI tests | prior tunnel/auth dispatch history; do not conflate with local sandbox | **KEEP** — remote transport/caller boundary |
| `C07` | Process-group cleanup | `processes.py::{run_bounded_process,terminate_process_group}`; `Runtime._terminate_session`, `_schedule_session_reaper`; `SharedJobRegistry` cleanup | all execution modes | Starts/controls process groups and terminates surviving descendants on cancel/close/failure; escalates TERM -> hard kill. | Yes, with fallback/reaper paths. | pathological unkillable kernel state can survive; not a permission bypass. | watchdog uses same primitive | measurable common exec-path work; exact share of ~390 ms not isolated in this audit | none normally; prevents leaked processes | `test_runtime_helpers` process-group termination/reaper tests | process cleanup has been a known DevMCP reliability focus; must not be removed for speed | **KEEP** — reliability boundary; profile separately for performance |
| `C08` | Timeout/watchdog | `ExecSession.timeout_at/refresh_status`; `start_session_watchdog`; exec deadline loop | all exec modes | Stops commands exceeding configured deadline. | Yes. | operator can request larger allowed timeout, bounded by schema; not infinite by default. | process cleanup | watchdog thread + deadline checks | timed-out result rather than indefinite hang | runtime-helper timeout/watchdog tests | watchdog lifecycle has regression coverage | **KEEP** — reliability boundary |
| `C09` | Output/session bounds | `SESSION_BUFFER_BYTES`; `MAX_ACTIVE_EXEC_SESSIONS`; `MAX_RETAINED_OUTPUT_SESSIONS`; `MAX_RUNTIME_OUTPUT_BYTES`; `truncate_output_bytes_tail`; HTTP request bound | all | Bounds retained memory/output and concurrent exec sessions; reports truncation. | Yes. | caller can choose output cap within schema, but global/session caps remain. | none | buffer/truncation accounting | truncated output/SESSION_LIMIT errors under load | output truncation/session limit tests; `OUTPUT_TRUNCATED` diagnostics | benchmark found control-plane starvation under concurrent compilation, supporting need for bounds rather than removal | **KEEP** — reliability/resource boundary |
| `C10` | Stateful preflight snapshot + drift check | `stateful_server.py::StateManagedRuntime._state_preflight/_guarded`; `state_snapshot.py::{collect_state_snapshot,DRIFT_FIELDS}` | state-managed mutating tools | Compares expected checkpoint to current branch/head/upstream/dirty/staged/untracked/content hashes before mutation. | Yes in state-managed runtime. | direct shell mutation is not serialized by this protocol; next managed mutation detects drift. | writer leases + file-level patch CAS | multiple Git/status/hash operations per managed mutation; not exec-command fast path | can return `STATE_DRIFT` and require caller refresh | `tests/test_state_management.py`; state routing tests | `b2ce3ea` added primitives; `9c11c6d`, `2d06ddf`, `b18eb27`, merged PR #23 fixed wiring/reliability | **KEEP** — state integrity |
| `C11` | Remote-ref verification after push | `state_remote.py::verify_remote_branch_head`; `StateMutationMixin.git_push` | structured Git push | Compares local HEAD with `git ls-remote --heads` result after push; raises `REMOTE_HEAD_MISMATCH`. | Yes for wrapped push. | raw shell Git intentionally bypasses wrapper guarantee. | state snapshot tracks remote-tracking head but is not same proof | one remote query after push | mismatch is explicit error | `tests/test_state_management.py` remote-head mismatch | recent state management fixes | **KEEP** — remote state integrity |
| `C12` | Writer leases | `writer_lease.py::{acquire_writer_leases,release_writer_lease}`; state preflight | state-managed branch mutations | Serializes DevMCP writers per branch with TTL/owner/logical task; detects active conflict and recovers stale lease. | Yes under project lock. | raw external Git/user process is outside lease protocol; drift detection catches resulting change. | state drift | lock + JSON file I/O per managed mutation | `WRITER_LEASE_CONFLICT` can defer competing writer | `tests/test_state_management.py` active/stale lease tests | added/fixed in PR #23 stack | **KEEP** — concurrency/state integrity |
| `C13` | Transaction apply conflict checks | transaction Git HEAD/branch checks + `AtomicPatchCommitter` | only explicit transaction apply | Ensures snapshot output does not overwrite concurrent branch/file changes. | Yes. | not used for normal direct full-access; intentionally opt-in. | C03/C10 at different scopes | Git subprocesses + baseline hashes only in transaction mode | transaction may fail with `TRANSACTION_CONFLICT` | transaction tests | protects WIP; no reason to put on every command | **KEEP inside explicit transaction feature**, not default execution sandbox |

## Current primary-source architecture comparison

This comparison is about architecture patterns, not copying implementations.

| System | Current primary-source behavior | Pattern relevant to DevMCP |
|---|---|---|
| **OpenAI Codex** | `SandboxMode::DangerFullAccess => PermissionProfile::Disabled`; the danger-full-access prompt says no filesystem sandboxing and all commands are permitted. | A resolved full-access mode should disable the permission sandbox/profile rather than keep a second hidden authority axis. Keep approval/protocol mechanisms conceptually separate from filesystem sandbox state. |
| **Claude Code** | `bypassPermissions` is one explicit permission mode; docs say it disables permission prompts and safety checks and allows protected-path writes. It deliberately retains a narrow root/home destructive-removal circuit breaker and explicit externally forced asks. | The important pattern is a single visible effective mode and an explicit list of exceptions. DevMCP’s target threat model is stricter than Claude’s circuit breaker: do not silently import that exception unless deliberately chosen. |
| **OpenCode** | A single `permission` configuration expresses `allow/ask/deny`, including a global `"permission": "allow"`; `--auto` is separately documented as only auto-approving asks while explicit denies remain. | Avoid two overlapping axes with unclear precedence. If compatibility auto-approve exists, define it as a transform on one canonical permission state, not another hidden ceiling. |
| **Cline** | YOLO explicitly auto-approves all file operations anywhere, all terminal commands including destructive ones, browser/MCP actions, and states that safety checks are disabled. | This is the closest documented user-facing semantic match for DevMCP target full-access: the UI warning communicates risk; runtime does not retain a covert workspace/command deny layer. |
| **Hermes Agent** | Security policy defines the OS as the actual adversarial-LLM security boundary and explicitly says in-process approval/pattern/tool allowlists are heuristics, not containment. Default terminal backend can run on host; stronger isolation is a separate backend/whole-process posture. | Treat regex/string command classifiers as UX/policy heuristics only. Keep real OS isolation as an explicit execution posture, separate from “trusted current-user host execution.” |

Primary sources checked on 2026-08-13:

- Codex danger-full-access prompt: <https://github.com/openai/codex/blob/main/codex-rs/prompts/templates/permissions/sandbox_mode/danger_full_access.md>
- Codex config derivation (`DangerFullAccess => PermissionProfile::Disabled`): <https://github.com/openai/codex/blob/main/codex-rs/config/src/config_toml.rs>
- Claude Code permission modes: <https://code.claude.com/docs/en/permission-modes>
- Claude Code permissions: <https://code.claude.com/docs/en/permissions>
- OpenCode permissions: <https://opencode.ai/docs/permissions>
- Cline Auto Approve / YOLO: <https://docs.cline.bot/features/auto-approve>
- Hermes Agent security policy: <https://github.com/NousResearch/hermes-agent/blob/main/SECURITY.md>
- Hermes Agent security guide: <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/security.md>

## Failure/history evidence relevant to deletion decisions

| Evidence | What it proves for this audit |
|---|---|
| `19d5758` — “Fix sandbox CI and policy compliance regressions” | sandbox policy is operationally expensive/fragile and must be isolated from full-access fast path rather than casually mixed with it |
| `13d4ffd` — “Align legacy policy tests and fix bwrap toolchain visibility” | legacy/profile compatibility and sandbox toolchain visibility have already required dedicated repair |
| `308fdc` — “deny nested sandbox escape commands” | hardcoded command denies were added to protect a sandbox threat model; they are not required by trusted-host full-access |
| `b0b9a7a` — “pin private sandbox environment in bwrap” | environment rewriting is part of sandbox semantics, not a universal current-user requirement |
| `a358187` — shell environment/MSVC inheritance work | aggressive env isolation caused real toolchain-compatibility pressure |
| `reports/benchmark/aider-polyglot-mcp-20260725.md` | JDK needed extra sandbox read roots; one Rust dependency error was falsely labeled `LANDLOCK_READ_ROOT_BLOCKED`; benchmark conclusion identified session lifecycle/sandbox toolchain config/resource isolation rather than patch landing as weaknesses |
| `65cff44` — cleanup removed dead in-process Landlock variant | precedent for deleting obsolete defense layers after proving no active requirement |
| `b2ce3ea`, `9c11c6d`, `2d06ddf`, `b18eb27`, merged PR #23 | state-management checks are recent reliability work, including a real MRO/recursion bug; deleting them as “security overhead” would regress a different problem class |
| `ef58775` | fake readonly annotations are explicitly a compatibility/UI escape hatch, not enforcement |

## Full-access target invariants

After the deletion plan, these assertions should be mechanically testable:

1. `full-access` executes as the current OS user without bwrap, Landlock, copied workspace, executor scheduler, capability approval, command classifier, sensitive-path command scan, or DevMCP privilege/container deny.
2. `sudo`, `su`, `doas`, `docker`, `podman`, `nsenter`, `bwrap`, setuid executables and absolute paths are decided by the OS/current-user permissions, not DevMCP policy.
3. Full-access structured file tools do not reject a path merely because it is outside the selected workspace or under `.git`, `.ssh`, `.env`, etc.; they may still canonicalize/validate the path for correct operation.
4. Full-access shell inherits the current host environment subject only to protocol-reserved variables that are required for DevMCP correctness; those reserved variables must be documented as tool protocol, not security policy.
5. Network in full-access is the current user/host network. Target filtering is an explicit sandbox/external-executor feature, not a hidden full-access restriction.
6. `permission_mode` and `policy_profile` do not independently constrain the same resolved operation. Status output exposes one canonical effective execution authority.
7. MCP/tunnel auth and Origin checks remain unchanged.
8. Process groups, cancellation, timeout, session/output bounds and cleanup remain unchanged unless separately optimized with reliability-equivalent tests.
9. Structured patch CAS/rollback remains. Removing destructive approval must not remove concurrent-edit protection.
10. Git/state writer leases, drift detection and remote-head verification remain.
11. Explicit transaction mode remains opt-in and never becomes a hidden default snapshot for full-access.

# TARGET DELETION MAP

The order is deliberate. Each step should land with tests before the next step so dead dependencies become obvious rather than being guessed.

| Order | Change | Exact source targets | Prerequisite / reason | Required test move |
|---:|---|---|---|---|
| `0` | Freeze non-sandbox boundaries as protected invariants | `processes.py`; `patching.py::AtomicPatchCommitter`; `stateful_server.py`; `state_snapshot.py`; `state_remote.py`; `writer_lease.py`; MCP auth/Origin/tunnel paths | prevents the simplification from deleting reliability/protocol/state checks | add/retain explicit tests tagged by behavior, not “security” bucket |
| `1` | Introduce one canonical resolved execution authority | `runtime_policy_from_args`, `Runtime.__init__`, server-info/check-exec reporting | all later deletions need one predicate such as `effective_execution_mode == "full-access"` | matrix test for every old input combination -> one resolved state; assert no contradictory status fields |
| `2` | Turn `permission_mode` into legacy input adapter only | `PERMISSION_MODE_CAPABILITIES`, `legacy_profile`, parser/config compatibility | removes the second post-resolution authority axis without breaking old CLI/config immediately | replace tests that assert two-axis behavior with adapter-equivalence tests |
| `3` | Add an early full-access bypass before local policy authorization | `exec_command`, `_profile_exec_command` | gives one auditable fast-path invariant: no capability/approval/classifier path in full-access | spy tests must assert zero `_policy_decision_for_capabilities`, zero ApprovalEngine calls, zero `_profile_authorize_command` |
| `4` | Remove full-access command denies | `_contains_always_denied_command`, `_reject_setuid_executable`, `_check_command_paths`, legacy sudo/su/doas scan in `_execute_command_legacy` | target model says OS user decides; must happen after canonical full-access is unambiguous | invert/delete `test_full_access_still_blocks_privilege_escalation`; add harmless fake executables proving DevMCP does not deny by basename/path |
| `5` | Remove full-access workspace/sensitive authority from structured file tools | `Workspace.resolve_existing_at`, `resolve_for_write_at`, `Runtime.resolve_*`, `path_security.py` call sites | shell/file-tool authority must converge; keep canonicalization separate first | new temp-tree tests outside selected workspace and for `.env`/`.git` paths under full-access; safe/trusted compatibility tests remain if modes retained |
| `6` | Bypass/remove grants and leases in full-access | `grant_root`, `grant_capability`, lease matching, task scopes | once file/command authority is host-level, leases add no authority in full-access | ensure old grant APIs either report compatibility-only/no-op clearly or are hidden from full-access catalog after deprecation |
| `7` | Remove full-access capability profile/ApprovalEngine decisions | `_profile_authorize_command`, `_profile_authorize_operation`, patch/Git approval call sites under full-access | depends on steps 3–6 so there is no hidden caller left | full-access destructive patch/Git operation tests must not return `approval_required`; CAS/state tests still pass |
| `8` | Remove full-access shell classifiers and network/env gates | `_command_domain_capabilities`, `_network_capability`, `_profile_command_capabilities`, `_leased_command_capabilities`, `NETWORK_RE`, full-access `_command_env` filtering call sites | classifiers no longer have a policy consumer in full-access | assert full-access env/network parity with host; retain compatibility tests for sandbox modes only |
| `9` | Fence bwrap/Landlock/inherited sandbox/executor selection behind explicit non-full-access posture | `landlock_enabled`, sandbox backend selection, `ExecutorRegistry`, inherited sandbox detection | ensures no regression can silently reintroduce a local sandbox into full-access | full-access spy tests: no `ExecutionSandbox.create`, no `get_bwrap_args`, no Landlock open, no executor scheduler |
| `10` | Keep snapshots only where semantics explicitly require them | `ExecutionSandbox.create`; transaction/container paths | normal nontransactional full-access must remain authoritative workspace; transaction/external executor can copy | large-workspace regression test ensuring normal full-access never snapshot-copies; transaction tests unchanged |
| `11` | Simplify patch destructive policy without touching CAS | `_analyze_patch`, `apply_patch` approval branch; preserve `FileBaseline`/`AtomicPatchCommitter` | separates “should user approve?” from “did file change concurrently?” | delete/compat-fence risk approval tests for full-access; keep PATCH_CONFLICT/rollback tests |
| `12` | Delete dead `ApprovalEngine.evaluate_command()` and stale architecture statement | `approval.py::evaluate_command`; `ARCHITECTURE.md` | safe only after live policy paths are simplified and global search remains zero callers | repository search test/static check optional; docs corrected |
| `13` | Retire explicit profile activation/legacy grants if no supported compatibility mode still consumes them | `activate_policy_profile`, policy persistence, lease/grant MCP schemas/catalog | final cleanup only; do not remove prematurely while safe/trusted compatibility is supported | schema/docs/API compatibility decision required |
| `14` | Profile/optimize process-session no-op separately | `spawn_process`, reader threads, `ExecSession.refresh_status/drain_readers`, finish/poll loop | measured ~390 ms exists even with zero policy decisions; deleting security code will not solve it | dedicated microbenchmark + process-descendant cleanup/timeout/output regression suite before any optimization |

### Expected dependency direction after cleanup

```text
remote caller
  -> MCP/tunnel auth + Origin                     [KEEP: protocol]
  -> tool dispatch
       -> resolved execution authority (single value)
            -> full-access
                 -> current-user host operation
                 -> process/timeout/output cleanup [KEEP: reliability]
                 -> patch/git CAS/state checks     [KEEP: integrity, when using structured mutators]
            -> optional safer/sandbox posture
                 -> capability/approval policy (if retained)
                 -> explicit bwrap/container/network/env isolation
       -> explicit transaction (optional)
            -> snapshot + CAS apply
```

There should be no arrow from `full-access` back into capability leases, path/sensitive deny, approval IDs, regex command classification, bwrap, Landlock, inherited-sandbox selection, or snapshot copying.

## Tests that must change vs tests that must not

### Expected to change/delete when full-access semantics are corrected

- `tests/compliance/test_runtime_helpers.py::test_full_access_still_blocks_privilege_escalation`
- `tests/test_release_prep.py::test_autonomous_profile_runs_arbitrary_exec_without_approval_but_not_sudo`
- `tests/compliance/test_autonomy_architecture.py::test_legacy_dangerous_mode_is_full_access_for_shell_only`
- tests asserting `permission_mode` still caps an explicit profile after canonical resolution
- full-access variants of root-grant/sensitive-path/patch-approval tests

### Must remain green through simplification

- MCP bearer/OAuth auth and Origin tests in `tests/compliance/test_mcp_contract.py`
- process-group, reaper, timeout, active-session and output truncation tests in `tests/compliance/test_runtime_helpers.py`
- patch `PATCH_CONFLICT`, rollback and baseline-limit tests
- `tests/test_state_management.py` writer lease / state drift / remote-head verification tests
- explicit transaction conflict/WIP-preservation tests
- sandbox tests for any **explicitly retained** safe/trusted/isolated mode; they should stop being interpreted as requirements for full-access

## Audit limitations / unresolved follow-ups

1. The audit proves source-level enforcement and dynamic policy-decision counts; it does not claim regex command classifiers can be exhaustively bypass-tested. They are structurally heuristic and should be treated as such.
2. The ~390 ms no-op execution cost is measured and clearly not explained by capability-policy lookup, but its exact split across process spawning, reader-thread/session draining, polling, formatting, and cleanup was intentionally not refactored in this docs-only audit.
3. Dedicated Git-tool environment isolation (`_git_env`) is deliberately separated from shell full-access. A follow-up must choose and document whether structured Git tools in full-access promise host-auth parity or a hermetic credential contract.
4. Compatibility policy remains a product decision: safe/trusted/profile modes can be retained as explicit opt-in postures, but they must no longer constrain resolved full-access or create misleading status.
5. Claude Code retains a narrow root/home delete circuit breaker in bypass mode; that is an upstream design choice, **not adopted here**, because it conflicts with this audit’s stated target threat model unless the DevMCP operator explicitly chooses such a circuit breaker later.

## Machine-state provenance

Before the audit branch was created:

- local branch: `main`
- local `HEAD`: `938f7398879c7f24a35a40f72a3dc153f60ca1e5`
- upstream: `origin/main`
- ahead/behind: `0/0`
- tracked/staged/untracked: clean
- installed DevMCP source/service SHA matched the same commit

The audit branch was created on GitHub exactly from that SHA. Local DevMCP branch create/switch write actions were denied by the currently installed tool safety gate, so source inspection and benchmarks were kept read-only on local clean `main`, and this docs artifact was written directly to the isolated GitHub audit branch. That tool-level gate is an integration constraint observed during the audit; it is not evidence that the corresponding repository layer is required by the target threat model.
