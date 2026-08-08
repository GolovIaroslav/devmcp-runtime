# Contributing

English is canonical for APIs, architecture, and contribution rules. Russian
documentation may explain the same workflow but must not invent a different
contract.

1. Create a focused branch from the line requested by the maintainer.
2. Keep MCP tool schemas compact and backward-compatible. Prefer task registry
   data and policy data to new model-facing tools.
3. Run `make lint`, `make typecheck`, focused tests, and `make test` before
   requesting review.
4. Do not commit tokens, private keys, `.env` files, runtime databases, logs,
   or generated local sandboxes.
5. Describe security impact, migration impact, and whether ChatGPT Developer
   Mode needs its app actions refreshed.

Small patches are preferred. Do not force-push shared branches.
