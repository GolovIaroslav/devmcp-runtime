# FAQ

**Is this an OpenAI product?** No. It is an independent MCP runtime.

**Does it require ChatGPT?** No. Any MCP client can use the core runtime.

**Is Power unrestricted host execution?** No. It only broadens configured
execution inside the selected sandbox; the minimum security floor remains.

**Where are secrets?** Outside the workspace under
`~/.config/devmcp-runtime/secrets/`, with `0600` permissions.

**Why is the tunnel optional?** Local MCP clients do not need it. It is only a
documented path for reaching a private local MCP server from supported clients.
