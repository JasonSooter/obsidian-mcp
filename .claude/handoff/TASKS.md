# Task register — durable, permanent record

**Why this file exists:** the Claude Code session task list is **not file-backed**. A task
created with `TaskCreate` and never written here is gone at `/clear` or session end. This
file is the durable copy. Rehydrate the session list from it; never the reverse.

Started 2026-11-01 with tasks moved from the
[home-media-server](https://github.com/JasonSooter/home-media-server) register, which
deploys this server; each row names its old ID there. Code work for this server is tracked
here; hosting it (compose, Tailscale, secrets on the box) stays in home-media-server.

**Next ID: T-006**

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
| T-004 | open | Publish the hero link-preview re-renders | #14 (2026-11-01) fixed black hero PNGs: resvg ignores CSS `var()`, so every v2 hero's preview rendered black; `RENDER_VERSION` 2 forces a re-render. A dry run on 2026-11-01 ~19:00Z staged **983 PNG updates** (renders cached) but also 15 `missing` hero SVGs and 14 PNG removals, because the map routine was mid-run, deleting and re-uploading heroes. **Next:** when the routine's `run_lock` in `Places/_Map Scan Progress.md` is clear, run `publish_site` (dry run): expect ~983 PNG updates and `missing_count` 0 (Airstream factory and Auschwitz may still be missing until remade). Then publish with the owner's OK. Live call takes minutes; the first run on 2026-11-01 lost its MCP transport mid-call, and the rerun answered from cache. |
| T-005 | open | Map routine adopts the hero tools | #15/#16 (deployed 2026-11-01, 7e4785e) added `start_hero_job` / `hero_job_status` / `get_hero` / `commit_heroes`; smoke test on the server: one hero in 13.8 s, contact sheet through the connector. **Owner:** paste the tool reference (in the 2026-11-01 handoff log) into a Claude Desktop chat with the scheduled task and give it OK to update its instructions. **Verify:** next run's progress note mentions start_hero_job/commit_heroes rather than "Dropbox delete + create_file", and the server audit log has `commit_heroes` entries. Known: Amazon rainforest needs `region: true` (Nominatim doesn't classify it as a region). |

---

## 📌 Standing facts that save re-derivation

- The OAuth code is shared by anki-mcp, obsidian-mcp and ynab-mcp: fix bugs in all three.
- **resvg ignores CSS custom properties.** Any SVG styled with `var(--x)` renders those fills and strokes as nothing. `render.resolve_css_vars` substitutes a theme first; bump `RENDER_VERSION` whenever render output changes for the same SVG, or cached PNGs are reused.
- **Hero map data** is built on first use into `<state>/hero/data` (311 MB). Downloads are slow on a home connection (~300 KB/s from GitHub, about 20 min); it persists, so this happens once.
- **Quartz Syncer won't delete frontmatter-only images by itself.** Read from Syncer 2.2.0's
  source on 2026-11-01: it counts an image as published only when a note's *body* embeds it,
  so images referenced only from frontmatter (`image:`, `socialImage:`) show up as "Unlinked
  media" in the Advanced tab of its publication center. They are deleted only if someone
  selects them there and clicks delete, runs Syncer's orphan clean with `force`, or turns on
  "Automatically clean orphaned media" (off by default; it also skips itself when more than
  80% of media look orphaned). So: leave that setting off and don't bulk-delete Unlinked media.

---

## ⚠️ False premises and method traps

- **`git checkout <file>` to undo a temporary edit throws away every uncommitted change in that file**, including the fix you were testing. Copy the file aside, mutate, then copy it back (2026-11-01: a whole render fix was lost once this way).
- **Never post "fixed in <sha>" from a command chain that also commits.** A signing failure (1Password locked) leaves HEAD on the old commit while the chain carries on; one review reply named the wrong SHA. Check the commit landed before replying.
- **zsh does not word-split `$var`** (`set -- $r` sets one argument); use `${=r}` or explicit lists. **npm 12** refuses git-hosted dependencies by default; `npm ci --allow-git=all` for repos whose lockfile pins one by commit.
- **Ignored files are expendable to git.** A checkout that brings a tracked file to a path
  you keep ignored (`.git/info/exclude`) overwrites the ignored copy without warning. That
  is how a session-local register at this file's path was lost on 2026-11-01. Never keep
  durable state at a path the repo might later track.

---

## 🗓️ Time-bound checks

- None.
