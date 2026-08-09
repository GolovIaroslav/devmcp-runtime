# MCP Desktop Client

This is the Python desktop-client MVP for `coding-tools-mcp`. It provides a
visual interface for:

- managing multiple workspaces;
- configuring externally hosted FRP or client-managed Cloudflare exposure;
- configuring OAuth, Bearer, or no authentication;
- starting and stopping a local MCP runtime;
- viewing runtime logs and the current endpoint; and
- copying the key fields required by a ChatGPT custom MCP app.

## Run

```bash
python -m pip install -e ".[desktop]"
coding-tools-mcp-desktop
```

You can also run it directly from a source checkout:

```bash
python apps/desktop-client/main.py
```

## Dependencies

- Python 3.11+
- PySide6
- psutil
- `uvx` or `coding-tools-mcp` available on `PATH`

## Languages

The client follows the system language on first launch. The bundled catalogs
currently include:

- English
- Simplified Chinese

The language can be changed from the left-side selector and the choice is
persisted through Qt settings. Unsupported system languages fall back to
English.

After changing UI text, refresh and validate the translation catalog with:

```bash
make desktop-i18n-update
make desktop-i18n-release
python scripts/check_desktop_i18n.py
```

## ChatGPT connection

When `oauth` is selected, the interface displays and lets you copy:

- connection URL
- OAuth client ID
- OAuth client secret
- authorization password

For FRP, configure the workspace, local port, FRP subdomain, and server
domain, then apply the generated snippet to `frpc` on the same host. The
desktop client manages only the local MCP runtime; it does not start or reload
`frpc`, and the displayed public URL is reachable only while `frpc` is running.

Cloudflare supports two modes:

- temporary tunnels, using `cloudflared tunnel --url` and an automatically
  assigned `trycloudflare.com` address;
- named tunnels, using a Tunnel Token and a configured public hostname.

## Current limitations

- FRP is externally managed; the client generates a configuration snippet but
  does not manage the `frpc` process.
- `Ngrok` and `Dev Tunnel` do not yet have real tunnel-start support.
- Cloudflare named tunnels require a tunnel and hostname configured in advance
  in the Cloudflare dashboard.
- For a Cloudflare named tunnel, the local service must match the ingress
  target, usually `http://127.0.0.1:<local-port>`.
