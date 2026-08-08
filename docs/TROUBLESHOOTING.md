# Troubleshooting

- `OpenAI Secure MCP Tunnel key is not configured`: local MCP still works;
  enter the runtime key once with `devmcp setup` or the local UI.
- `SANDBOX_UNAVAILABLE`: install and verify `bwrap`; do not rely on an
  unannounced unsafe fallback.
- `approval_required`: inspect `devmcp approvals`, approve the exact request,
  and retry once. Replaying a consumed request must fail.
- ChatGPT cannot see a changed action: refresh the draft app actions and run
  **Scan Tools** again.
- Health is not readiness: check both `devmcp status` and
  `tunnel-client doctor --explain`.
