# ChatGPT connection

DevMCP Runtime can be connected to ChatGPT as a custom MCP app when the
workspace and account support developer mode. Keep the MCP endpoint private,
use the generated token file, and expose it only through the supported secure
tunnel workflow.

## Setup

1. Run `devmcp setup` and complete the local health check.
2. Run `devmcp tunnel status` and follow the tunnel-client instructions for the
   authenticated runtime connection.
3. In ChatGPT, create or edit a custom app and add the MCP endpoint and
   authentication configured by the tunnel flow.
4. Refresh the app's tools after changing the server card or schemas. A local
   policy change or service restart alone does not require Refresh; the MCP
   bearer token is preserved unless you explicitly rotate it.
5. Confirm that a harmless read-only tool works before enabling write tools.

ChatGPT's current UI and account capabilities determine which connection
options are available. See the official guidance:

- [Developer mode and full MCP connectors](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt)
- [OpenAI Secure MCP Tunnel client](https://github.com/openai/tunnel-client)

Never paste the MCP token or control-plane API key into a prompt, issue,
screenshot, log, or public repository.
