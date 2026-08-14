# Troubleshooting

- `OpenAI Secure MCP Tunnel key is not configured`: local MCP still works;
  enter the runtime key once with `devmcp setup` or the local UI.
- `SANDBOX_UNAVAILABLE` from an older PLAN/sandbox compatibility path is not a BUILD requirement; current BUILD uses direct host execution.
- BUILD does not return per-command `approval_required`; unexpected permission errors should be diagnosed as OS/runtime/tool-contract failures.
- ChatGPT cannot see a changed action: refresh the draft app actions and run
  **Scan Tools** again.
- Health is not readiness: check both `devmcp status` and
  `tunnel-client doctor --explain`.
