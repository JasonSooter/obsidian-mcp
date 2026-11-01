# Task register — durable, permanent record

**Why this file exists:** the Claude Code session task list is **not file-backed**. A task
created with `TaskCreate` and never written here is gone at `/clear` or session end. This
file is the durable copy. Rehydrate the session list from it; never the reverse.

Started 2026-11-01 with tasks moved from the
[home-media-server](https://github.com/JasonSooter/home-media-server) register, which
deploys this server; each row names its old ID there. Code work for this server is tracked
here; hosting it (compose, Tailscale, secrets on the box) stays in home-media-server.

**Next ID: T-004**

---

## 🎯 Priority

None set. Pick from the register.

---

## Register

| ID | Status | Subject | Detail |
|----|--------|---------|--------|
| T-001 | open | Port obsidian-mcp from Python to TypeScript | **Why:** Jason reads TypeScript far more easily than Python (asked 2026-09-26). Run on the current Node LTS in Docker (`node:<lts>-slim` pinned by digest). Official MCP TypeScript SDK; `gray-matter`/`yaml` for frontmatter; OpenTelemetry JS with the same attribute names so the Grafana dashboards and alerts keep working. **Order:** (1) port `tests/test_core.py` (~950 lines) to `node:test` first, so it is the spec; (2) port the modules bottom-up: `vault` → `notes`/`links`/`search` → `journal` → `quartz` → tools → server; (3) do OAuth (`oauth/provider`, `store`, `totp`, `login`) last and most carefully. **Must not change:** tool names, input schemas and result shapes (so connected clients notice nothing); the formats of the version journal, the OAuth store and the publish state, which a new server must read as-is. Cut over by changing the compose service in home-media-server; roll back to the previous image. Probably 3–4 PRs. (Was home-media-server T-045.) |
| T-002 | open | Decide `AuthSettings.validate_token_resource` before mcp SDK 3.0 | Every server logs `MCPDeprecationWarning: AuthSettings.validate_token_resource is not set`. In mcp SDK 3.0 it defaults to True whenever `resource_server_url` is set. The providers already store and return the token's `resource` (pending login → authorization code → token store → refresh → `AccessToken(resource=...)`), so the open question is whether that stored value matches what the SDK will check. Verify that in all three repos together (anki-mcp, obsidian-mcp, ynab-mcp; the OAuth code is shared), then set the flag explicitly rather than inherit 3.0's default. No work started; checked 2026-11-01: not set anywhere. (Was home-media-server T-060.) |
| T-003 | blocked | Local (stdio) mode + Fly.io template | **Deferred by Jason 2026-09-26** ("skip that for now"). Serverless (Vercel, Lambda, Workers) doesn't fit either server as built: anki-mcp needs a persistent disk, a single writer and Anki's Rust-backed Python library; obsidian-mcp needs the vault on disk. The easiest paths for others: (1) stdio mode for Claude Desktop (a few lines each: both build an `MCPServer` and serve `streamable_http_app()`; stdio would serve the same server over stdio, and skips OAuth); (2) a Fly.io/Railway template with one volume and max 1 instance. A Workers version of obsidian-mcp would need a storage-API redesign; revisit during the TypeScript port. Applies to anki-mcp and obsidian-mcp. (Was home-media-server T-054.) |

---

## 📌 Standing facts that save re-derivation

- The OAuth code is shared by anki-mcp, obsidian-mcp and ynab-mcp: fix bugs in all three.

---

## ⚠️ False premises and method traps

- None recorded yet.

---

## 🗓️ Time-bound checks

- None.
