# Permission Modes (Legacy Ingress Adapters)

The `--permission-mode` flag is a thin ingress-only adapter for backwards compatibility.
New deployments should use `--execution-mode` directly.

## Mapping

| `--permission-mode` | Resolved execution_mode | effective_access |
|---|---|---|
| `safe` | `plan` | `read-only` |
| `trusted` | `build` | `full-access` |
| `dangerous` | `build` | `full-access` |

## Current Preferred Flags

```bash
# Preferred
coding_tools_mcp --execution-mode build
coding_tools_mcp --execution-mode plan

# Legacy compat (still accepted)
coding_tools_mcp --permission-mode trusted  # => build
coding_tools_mcp --permission-mode safe     # => plan
```

The mode is resolved once at startup. There is no runtime approval gate or profile
matrix applied after resolution.
