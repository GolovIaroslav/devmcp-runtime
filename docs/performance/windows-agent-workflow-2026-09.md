# Windows agent workflow measurement

Measured on 2026-09-09 in an isolated Windows temporary Git repository with
the local `StateManagedRuntime`. The running DevMCP service, Minecraft, and
other contexts/jobs were not contacted.

Scenario: `list_projects` → `select_project` → one `read_files` batch →
`apply_patch` → `ruff format` with `state_effect="selected_repo"` → background
targeted `pytest` → terminal `job_status`. The baseline additionally called
`job_output`, because the old `job_status` returned no output. The after run
requested `include_output=true, preview_bytes=2048` on the terminal status.

| Metric | Before | After | Change |
| --- | ---: | ---: | ---: |
| MCP tool calls | 8 | 7 | -1 |
| Retries/repeats | 0 | 0 | unchanged |
| Total JSON response bytes | 11,691 | 11,036 | -655 (-5.6%) |
| Target test exit code | 0 | 0 | unchanged |
| Formatted file SHA-256 | `ff26af5b757d54778d408d9ee60025bad6ee79a3d0b1b1010ae98b14b17f2cc6` | same | unchanged |

The measured server-side call times in milliseconds were:

| Call | Before | After |
| --- | ---: | ---: |
| `list_projects` | 3.80 | 4.09 |
| `select_project` | 60.06 | 65.87 |
| `read_files` | 24.39 | 10.68 |
| `apply_patch` | 1,618.62 | 2,263.63 |
| formatter `exec_argv` | 1,407.66 | 1,424.18 |
| targeted test start `exec_argv` | 32.55 | 26.31 |
| terminal `job_status` | 942.62 | 772.57 |
| removed `job_output` | 1.67 | — |

The preview is opt-in and bounded to at most 4096 bytes. It reads retained
buffers without advancing output cursors; `job_output` and `read_output` remain
available for full retained stdout/stderr. The generated non-interactive
`next_action` requests the bounded preview, while interactive sessions still
use `write_stdin`.

Limitations: this is one before/after run through the in-process runtime, not
the plugin/gateway path, a model/client wall-clock measurement, or the running
service. Process startup and filesystem scheduling dominate several timings.
The result supports one fewer server call and a smaller response for this
workflow; it does not establish a general plugin speedup or a Codex-equivalent
wall time. Linux compatibility was not executed in this Windows environment.
