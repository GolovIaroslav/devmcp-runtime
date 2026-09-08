# Windows recovery investigation (2026-09-08)

## Evidence

- At entry no DevMCP Python service or historical pytest PIDs were running.
- Primary checkout was `45123ff`; candidate checkout was clean `3f211c5`.
  The primary was fast-forwarded without rewriting either commit. The CRT
  stdout/stderr protection already existed in `45123ff`; this was not a new fix.
- Candidate full pytest completed in 267.58 seconds: 280 passed, 55 failed,
  160 skipped, 626 subtests passed. It did not hang. Failures include CRLF
  expectations and POSIX shell assumptions (exit 127). This is not a green
  cross-platform acceptance result.
- The running candidate passed 100 real loopback HTTP initialize/exec/health/
  DELETE cycles with four concurrent clients. Historical remote 502 / Session
  terminated symptoms were not reproduced by this workload.
- A separate real-pipe probe reproduced another cleanup deadlock: a 1 MiB stdin
  write to a child that does not read blocks `close_process_streams()`, even with
  the previous stdout/stderr fix. The close returned only after killing the child.
- MCP service restart AND update unconditionally required `systemd-run` despite
  an existing Windows CLI lifecycle implementation.

## Changes

- Serialize stdin writes and defer stdin closure to the writer when it owns the
  stream. Cleanup never waits for the CRT/BufferedWriter lock. The existing
  timeout/process termination still owns cancellation of a blocked write.
- Schedule Windows service actions through a detached, delayed PowerShell helper
  using the existing launcher. Encoded scripts preserve arguments and spaces.
  Linux scheduling remains unchanged. Linux scheduler tests explicitly select
  the Linux path; Windows tests exercise a real delayed CLI invocation.
- Enabled repository-local `core.longpaths=true` on the primary checkout; no
  machine-wide Git setting or state-routing policy was changed.

## Validation and limits

- Targeted Windows pipe, HTTP transport and release lifecycle tests:
  **54 passed, 2 skipped**. Ruff passes on changed Python files.
- The blocked-stdin test establishes that a writer holds its lock, verifies it
  remains blocked, requires cleanup within 0.5 seconds, and checks final closure.
- The new stdin defect is reproduced; it is not proven to explain the historical
  remote failures. Do not claim all Windows defects resolved from these tests.
- Multiple logical contexts deliberately use linked worktrees under contention;
  their existence alone is not proof of incorrect routing. Do not force all
  concurrent contexts into one checkout or delete dirty worktrees.
- Installed build metadata previously named an older main SHA while development
  startup used the editable checkout. Verify installation and source identity
  after installation, rather than trusting a Git HEAD or metadata alone.
