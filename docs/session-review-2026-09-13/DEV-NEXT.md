# DevMCP continuation entrypoint

This file is the stable entrypoint for the DevMCP/Cavendish infrastructure track.
Software Factory is a separate project and must not be executed from this track.

## Bootstrap

1. Call DevMCP `server_info` read-only without reusing an old `context_id`.
2. Reuse the returned fresh `context_id` consistently for the bounded segment.
3. Read `DEV-STATE.json` in this directory.
4. Reconcile the live branch/HEAD/status, source versus installed runtime identity,
   MCP health, and tunnel readiness before treating any historical note as current.
5. Read the relevant durable continuation checkpoint only if
   `DEV-STATE.json` selects a concrete task. A stale checkpoint is evidence, not a
   selector.
6. Use only DevMCP-owned coordination state when writing this track. Never edit
   `/home/jar/.local/state/software-factory/coordination/software-factory.md`.

## Current routing

The D01-D05 infrastructure/Cavendish packet is COMPLETE.

- D01: COMPLETE.
- D02: COMPLETE.
- D03: COMPLETE.
- D04: COMPLETE.
- D05: COMPLETE.
- PR #62 repository-scoped writer isolation: merged and accepted.
- Canonical DevMCP runtime-code baseline at reconciliation start: branch
  `fix/antigravity-windows-argv`, commit
  `c3727526be5e2b8466d280653dada9341ebcea29`. A continuation-only docs commit
  may advance Git HEAD without changing runtime code.
- Installed DevMCP runtime: commit
  `c3727526be5e2b8466d280653dada9341ebcea29`; health is OK and tunnel is ready.
- Canonical Cavendish source:
  `b1965984a645075ab7950cb62cf669bb81831296`.

Do not route back to D01-D05. Do not restore the historical `WAIT_EXTERNAL`
state. Do not repeat D01 installation/restart/review/Factory ACK, D02/M01,
D03/M02, D04 live integration/acceptance, or D05 closeout.

There is currently no selected DevMCP implementation task. The exact next bounded
step is **backlog reconciliation only**:

1. Reconcile the surviving broader DevMCP backlog against current source and the
   accepted packet.
2. Start from `ROADMAP.md`, `TASKS.md`, and the preserved pre-update review
   material under
   `/home/jar/.local/state/devmcp-linux-preupdate-untracked-20260922/session-review-2026-09-13/`.
3. Treat historical A01-A97, optional `operation_key`, production memory/graph,
   release/onboarding, and other deferred candidates as candidates only. Do not
   choose one merely because it appears in an old plan.
4. Select a new current task only when surviving evidence plus current source
   proves the task is still unfinished, relevant, and not superseded.
5. Save the selected task and exact first unfinished acceptance step into
   `DEV-STATE.json` before implementation. If no task can be selected
   defensibly, leave `current_task` null and record the unresolved decision.

Do not implement the newly selected backlog item in the same reconciliation
segment.

## Evidence and recovery

The pre-update copy of the former untracked continuation tree survives at:

`/home/jar/.local/state/devmcp-linux-preupdate-untracked-20260922/session-review-2026-09-13/`

Its `DEV-STATE.json` still selected D02 and is historical only. The current
project's durable checkpoint store likewise contains old D01/D02 records; they are
not authority for routing after the accepted D05 closeout.

Useful current or surviving evidence:

- `README.md`
- `ROADMAP.md`
- `TASKS.md`
- `docs/AGENT_AUTONOMY.md`
- `/home/jar/.local/state/software-factory/coordination/devmcp.md`
- `/home/jar/.local/state/software-factory/coordination/software-factory.md`
  (read-only peer evidence)
- `/home/jar/.local/state/linux-sync-20260922/devmcp-status.txt`
- `/home/jar/.local/state/linux-sync-20260922/devmcp-tracked-wip.patch`
- `/home/jar/Documents/projects/Cavendish/.git/refs/heads/fix/cavendish-composer-repair3`

Software Factory state is separate. Its S01 is complete and its next task is S02;
do not execute S02 from this DevMCP track.

Pre-existing untracked `antigravity-image-test/` and `results/` are not owned
by continuation restoration. Preserve them unless a later task explicitly claims
them.

## Rules for future bounded segments

- One bounded segment per chat.
- Never infer CURRENT from an old timestamped report without reconciling live
  state.
- Never turn an old blocker into a current blocker without fresh evidence.
- Keep source changes, installation, and runtime restart separate. The runtime is
  already accepted at the current source SHA, so do not reinstall or restart it
  merely to resume work.
- Preserve foreign dirty files and other project ownership.
- Before declaring a task complete, run the task's applicable verification and
  save the exact next unfinished step.
