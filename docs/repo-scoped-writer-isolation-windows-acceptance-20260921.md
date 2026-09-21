# Windows acceptance вЂ” repository-scoped writer isolation

Date: 2026-09-21

## Candidate

Repository: `GolovIaroslav/devmcp-runtime`

Reviewed commit:
`d1f8ccedf2aed9fabb9b17aa9f66f3609a6c4363`

Parent:
`91823a9b824405038cc22f3724a009860cbfd596`

Source branch:
`fix/repo-scoped-writer-isolation`

Draft PR:
`#62`

## Independent review

A separate read-only reviewer reported PASS with no blocking ownership defect.

The independent adversarial checks covered:
- different repositories concurrently: both writers stayed on their canonical checkouts;
- same repository with two explicit writers: second writer isolated;
- sequential stateless HTTP: no phantom writer after request release;
- genuinely concurrent stateless same-repo requests: later writer isolated;
- explicit context durability;
- A -> B -> A project switching without cross-repository owner leakage;
- stale/pruned context behavior;
- eviction pressure preferring transient stateless eviction before an older explicit owner;
- managed-worktree / absolute canonical path protection.

Reported targeted verification:
- `tests/compliance/test_session_state.py`: 15/15 PASS;
- targeted HTTP ownership regressions: 4/4 PASS;
- targeted managed-worktree regression: 1/1 PASS;
- independent registry invariant suite: PASS;
- `git diff --check 91823a9..d1f8cced`: PASS.

A broader Windows HTTP test still has the known unrelated CRLF assertion (`a\r\n != a\n`); its ownership assertions were not the failing part.

## Official Windows installation

The canonical checkout was clean on branch:
`fix/repo-scoped-writer-isolation`

It was fast-forwarded from `91823a9` to exact reviewed `d1f8cced`.

DevMCP's own `service_update` operation was then run with:
- source project: `devmcp-runtime`;
- `development_mode=true`;
- expected SHA resolved by the runtime: `d1f8ccedf2aed9fabb9b17aa9f66f3609a6c4363`.

Fresh post-update runtime identity:
- runtime SHA: `d1f8ccedf2aed9fabb9b17aa9f66f3609a6c4363`;
- source branch: `fix/repo-scoped-writer-isolation`;
- dirty build: no;
- MCP process: running;
- MCP health: ok;
- execution mode: BUILD / full-access / host;
- tunnel process: running;
- tunnel ready: yes;
- tunnel id: `tunnel_6a771229f2e48191b34d642ea92892c8`.

Fresh `local_state_snapshot` also reported:
- canonical HEAD = `d1f8cced...`;
- installed service SHA = `d1f8cced...`;
- `installed_matches_source=true`;
- no dirty/staged/untracked paths.

## Live post-install ownership smoke

All checks below used the installed/restarted runtime, not a test checkout.

### 1. Sequential stateless same-repo requests

First stateless mutation added a unique smoke file in:
`C:\other\projects\devmcp-runtime`

Second independent stateless mutation updated the same file.

Both results reported workspace exactly:
`C:\other\projects\devmcp-runtime`

The second request did not enter a managed worktree, proving the first completed stateless request did not remain a phantom writer.

### 2. Explicit writers in different repositories

Two explicit contexts mutated concurrently:

Repo A:
`C:\other\projects\devmcp-runtime`

Repo B:
`C:\Users\mrgol\Desktop\minecraft-castle-automation`

Results:
- A workspace remained the canonical DevMCP checkout;
- B workspace remained the canonical Minecraft checkout;
- neither workspace contained the DevMCP managed-worktree path.

This is the live scenario required for a DevMCP agent and an unrelated repository agent to work in parallel.

### 3. Explicit writers in the same repository

Two fresh explicit contexts targeted the DevMCP repository.

First writer result:
`C:\other\projects\devmcp-runtime`

Second writer result:
`C:\Users\mrgol\.config\devmcp-runtime\state-management\f94baddfd0b97cacc1aa34dea51419e6d9383ea4fba24686f9ee962f334aff21\worktrees\968731d8dffa4369126c4a2c`

Filesystem verification before cleanup showed:
- first-writer file existed in canonical;
- second-writer file existed in the managed worktree;
- second-writer file did NOT exist in canonical.

Therefore the same-repository exclusion/isolation invariant remained intact.

## Cleanup and final state

Every smoke file was removed by exact path.

After cleanup:
- canonical DevMCP `git status --porcelain --branch`: clean;
- Minecraft automation `git status --porcelain --branch`: clean;
- same-repo smoke contexts were switched away from DevMCP to release their durable mutation ownership.

Fresh final runtime verification:
- health: ok;
- runtime/install SHA: `d1f8ccedf2aed9fabb9b17aa9f66f3609a6c4363`;
- source/install identity matches;
- tunnel ready: yes;
- DevMCP working tree clean.

## Rollback identity

The exact pre-fix parent is:
`91823a9b824405038cc22f3724a009860cbfd596`

If rollback is required, use a clean pinned checkout of that commit through the same supported DevMCP update/install path, then verify reported installed SHA, health and tunnel readiness before resuming mutation work.

## Verdict

PASS for the repository-scoped ownership fix on the installed Windows runtime.

Different repositories can mutate concurrently without false contention. Real simultaneous writers of one canonical repository remain isolated. Completed stateless HTTP requests do not leave phantom ownership.
