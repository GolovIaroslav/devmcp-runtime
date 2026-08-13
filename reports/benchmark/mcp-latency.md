# MCP Runtime Latency Benchmark

- Conclusion: **PASS**
- Endpoint: `http://127.0.0.1:44665/mcp`
- Iterations: `10`
- Exec iterations: `20`
- Warmup iterations: `2`
- Max MCP p95 threshold: `5000 ms`
- Unrelated tree: `4000` files / `8192000` bytes
- `true` p50 after unrelated tree: `2.929 ms` delta / `1.124` ratio

## Metrics

| metric | samples | min ms | p50 ms | p95 ms | max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| `mcp.tools_list` | 10 | 1.941 | 2.141 | 2.701 | 2.853 |
| `mcp.read_file` | 10 | 1.033 | 1.107 | 1.296 | 1.319 |
| `mcp.search_text` | 10 | 5.869 | 6.603 | 7.621 | 7.69 |
| `mcp.exec_true` | 20 | 22.783 | 23.567 | 24.159 | 24.421 |
| `mcp.exec_argv_true` | 20 | 3.651 | 23.967 | 24.87 | 25.278 |
| `mcp.exec_python_pass` | 20 | 44.412 | 44.97 | 45.677 | 45.983 |
| `mcp.git_status` | 20 | 5.782 | 6.168 | 7.834 | 7.944 |
| `mcp.exec_pytest_small` | 20 | 205.755 | 225.818 | 247.232 | 247.297 |
| `native.read_text` | 10 | 0.018 | 0.019 | 0.042 | 0.047 |
| `native.search` | 10 | 4.231 | 4.56 | 5.195 | 5.377 |
| `native.exec_true` | 20 | 0.719 | 0.845 | 0.937 | 0.978 |
| `mcp.exec_true_after_unrelated_tree` | 20 | 5.282 | 26.496 | 27.423 | 27.49 |

## Native Baseline Comparison

| operation | MCP p95 ms | native p95 ms | ratio |
| --- | ---: | ---: | ---: |
| `read_file` | 1.296 | 0.042 | 30.857 |
| `search_text` | 7.621 | 5.195 | 1.467 |
| `exec_true` | 24.159 | 0.937 | 25.783 |

## Failures

No failures recorded.

## Notes

- Native baselines are local developer-tool primitives, not equivalent MCP substitutes.
- Latency thresholds are intentionally broad; this smoke benchmark catches transport regressions and records trend evidence.
