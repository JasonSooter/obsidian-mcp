# obsidian-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude — Claude Code,
Claude Desktop, claude.ai and the mobile apps — read/write access to an Obsidian
vault, over Streamable HTTP.

It is **vault-native**: it works directly on the markdown files, so it needs no
Obsidian app, no plugin and no GUI. Run it on a headless server next to a copy of
your vault kept in sync by Dropbox, rclone, Syncthing, Obsidian Sync or git, and
one URL works from every Claude client.

## Why not the existing Obsidian MCP servers

The community server ([MarkusPfundstein/mcp-obsidian]) and Obsidian's own
official CLI are both good, and both are the *right* tool on a desktop. Neither
can serve a headless machine, for the same two reasons:

| | Needs Obsidian running | Transport |
|---|---|---|
| MarkusPfundstein/mcp-obsidian | Yes — it is an HTTP client for the Local REST API **plugin** | stdio |
| Obsidian official CLI (1.12+) | Yes — documented as "a remote control for a running app"; launches it if absent | n/a |
| Obsidian Headless (beta) | It is a **Sync client**: replicates files, does not run the app or its plugins | n/a |
| Quartz Syncer plugin CLI | Yes — "Obsidian must be running for CLI commands to work" | n/a |
| **this server** | **No — reads the markdown on disk** | **Streamable HTTP** |

A headless server has no GUI session for Obsidian to run in. And stdio has no
listening socket, so it cannot serve a phone or a browser at all.

Keep using a plugin-based server on your desktop if you like — same vault,
different access path, no conflict.

[MarkusPfundstein/mcp-obsidian]: https://github.com/MarkusPfundstein/mcp-obsidian

## Quick start

You need Docker, a folder containing your vault(s), and an authenticator app.

1. **Create the login secrets.** The server refuses to start without a password
   (12+ characters) and a TOTP secret:

   ```bash
   cp secrets.env.example secrets.env
   # OBSIDIAN_MCP_LOGIN_PASSWORD: e.g. `openssl rand -base64 24`
   # OBSIDIAN_MCP_TOTP_SECRET:    a base32 secret; add the same one to your
   #                              authenticator (1Password, Aegis, Google Authenticator...)
   ```

2. **Pre-create the state directory**, owned by uid 1000. The container runs as
   that user and refuses to start if `/state` is unwritable — deliberately, since
   serving the write tools with no undo is the situation the journal prevents:

   ```bash
   mkdir -p state && sudo chown -R 1000:1000 state
   ```

   The vault folder must be readable and writable by uid 1000 as well.

3. **Run it.** `OBSIDIAN_VAULTS` names the vault folder(s) under the mounted root:

   ```yaml
   # compose.yaml
   services:
     obsidian-mcp:
       image: ghcr.io/jasonsooter/obsidian-mcp:main
       env_file: secrets.env
       environment:
         OBSIDIAN_VAULTS: MyVault
         OBSIDIAN_MCP_PUBLIC_URL: https://<your-host>.<your-tailnet>.ts.net
       volumes:
         - /path/to/vaults:/vaults      # contains MyVault/
         - ./state:/state
       ports:
         - "127.0.0.1:8780:8780"
       restart: unless-stopped
   ```

   ```bash
   docker compose up -d
   curl -s localhost:8780/healthz     # {"status":"ok","vaults_reachable":true}
   ```

Images are published for `linux/amd64` and `linux/arm64`: `:main` tracks the
main branch, `:sha-<commit>` pins a build. Pin by digest in production.

## Configuration

| Variable | Default | |
|---|---|---|
| `OBSIDIAN_VAULTS` | — | **Required.** Comma-separated vault folder names under `OBSIDIAN_VAULT_ROOT` |
| `OBSIDIAN_MCP_LOGIN_PASSWORD` | — | **Required.** Login password, ≥12 characters |
| `OBSIDIAN_MCP_TOTP_SECRET` | — | **Required.** Base32 TOTP secret (the setup key, not a 6-digit code) |
| `OBSIDIAN_MCP_PUBLIC_URL` | `http://localhost:<port>` | The public base URL clients reach. Becomes the OAuth `issuer`: set it exactly |
| `OBSIDIAN_DEFAULT_VAULT` | first of `OBSIDIAN_VAULTS` | Vault used when a tool call names none |
| `OBSIDIAN_VAULT_ROOT` | `/vaults` | Directory holding the vault folders |
| `OBSIDIAN_MCP_STATE_DIR` | `/state` | Version journal, OAuth database, audit log |
| `OBSIDIAN_MCP_HOST` / `OBSIDIAN_MCP_PORT` | `0.0.0.0` / `8780` | Listen address |
| `OBSIDIAN_MAX_FILE_BYTES` | `2000000` | Files larger than this are refused |
| `OBSIDIAN_JOURNAL_RETENTION_DAYS` | `30` | Full undo history kept this long, then thinned to daily |
| `OBSIDIAN_MCP_ENV` | `production` | `deployment.environment.name` on telemetry |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` | unset | Optional OTLP log export. Unset = log to stdout only |
| `QUARTZ_ENABLED` | `false` | Turn on the Quartz publishing tools |
| `QUARTZ_REPO_PATH` | `/quartz` | Checkout of your Quartz site repo |
| `QUARTZ_SSH_KEY_PATH` | unset | Deploy key used to push the site repo |
| `QUARTZ_GIT_REMOTE` / `QUARTZ_GIT_BRANCH` | `origin` / `main` | Where publishing pushes |
| `QUARTZ_GIT_AUTHOR` | `obsidian-mcp <obsidian-mcp@localhost>` | Commit author for publishes |
| `QUARTZ_BUILD_LOCALLY` | `true` | Run `npx quartz build` before pushing. The image has no Node, so set `false` when your host builds on push |

**Port 8780, not 8770**, so it can run beside
[anki-mcp](https://github.com/JasonSooter/anki-mcp) on 8770. Under some Docker
runtimes (Colima, for one) an occupied host port fails silently — the container
looks healthy while every request reaches the other service.

## The rule that makes this safe

The vault is **shared mutable state**: your sync tool writes to it whenever it
likes, and this server is not holding a lock. So:

- **Nothing is cached.** Every tool call reads fresh from disk.
- **Writes are atomic**: temp file in the same directory, `fsync`, `os.replace()`.
  Temp files are named `.obsidian-mcp.tmp.*` — tell your sync tool to ignore
  that pattern.
- **Every mutation is snapshotted first** into `/state/versions/`, outside the
  vault. `list_versions` and `restore_version` undo any append, edit, overwrite,
  or delete. `vault_delete` also moves the note to `.trash/` rather than
  unlinking it.

The only action here that cannot be undone is `publish_site`, because content on
a public website is public. It defaults to `dry_run=True`.

## Security posture

This runs as a **single profile with every tool**, typically published to the
internet (e.g. with Tailscale Funnel), because that is what makes one connector
URL work on every device — phones and claude.ai connect from Anthropic's
infrastructure and have no route into a private network.

That means `vault_write`, `vault_delete`, and `publish_site` are internet-reachable
behind one login. Controls, none of which cost capability:

- OAuth 2.1 with a password **and** TOTP login; the server refuses to start
  without both (see [Authentication](#authentication)).
- Version journal — everything but publishing is reversible.
- Hard path confinement: symlinks are resolved *before* the containment check,
  so a symlink planted in the vault is rejected exactly like `../../etc/passwd`.
  Extension allowlist (`.md`, `.markdown`, `.txt`, `.canvas`); `.obsidian/` is
  never readable, listable, or searchable.
- `publish_site(dry_run=True)` by default.
- Repo-scoped **deploy key** for publishing, never a personal access token — a
  container compromise is not an account compromise.
- Append-only `/state/audit.log` of every mutation, never readable via any tool.
- `QUARTZ_ENABLED=false` (the default) unregisters the publishing tools entirely.

## Tools

Tool names deliberately match the plugin-based Obsidian connector, so prompts
and habits carry across both servers.

| Tool | Notes |
|---|---|
| `vault_read` | frontmatter, body, tags, links, and `mtime_ns` |
| `vault_list` | by folder; sorted by `modified` (default), `created`, or `path` |
| `vault_get_document_map` | vault outline: paths, titles, tags, headings. The orientation tool |
| `search_simple` | literal text search |
| `search_query` | regex, plus `tag` and `properties` (frontmatter) filters |
| `tag_list` | every tag with a usage count |
| `vault_append` | end of file, or end of a named `heading` section. The capture tool |
| `vault_patch` | precise: `heading` (incl. `Parent::Child`), `^block-id`, or frontmatter key × append/prepend/replace |
| `vault_write` | `mode="create"` (default, refuses to clobber) or `"overwrite"`; takes `expected_mtime_ns` |
| `vault_move` | rename/move, **repointing every backlink** by default |
| `vault_copy` | duplicate a note; its links are left pointing where they pointed |
| `vault_delete` | moves to `.trash/`; journalled |
| `resolve_links` / `list_backlinks` | wikilink graph, shortest-unique-path resolved |
| `list_versions` / `restore_version` | undo, including for deleted notes |
| `set_publish_status` / `list_published` / `preview_publish` / `publish_site` | Quartz; only when `QUARTZ_ENABLED=true` |

### Beyond the plugin-based connector

`resolve_links`, `list_backlinks`, `list_versions`, and `restore_version` have no
equivalent there — the link graph and the undo journal are additions.

### What cannot be done vault-natively

Four tools on the plugin-based connector drive the Obsidian **user interface**,
so they have no meaning without a running app and are deliberately absent:

| Missing | Why |
|---|---|
| `command_list`, `command_execute` | run Obsidian commands — there is no app to run them in |
| `open_file` | opens a file in the Obsidian UI |
| `active_file_get_path` | asks which file the UI currently has open |

Everything else that connector does is covered above. Use the plugin-based
connector when you want to drive the app; use this one to work with the vault's
contents.

### Link resolution

`list_backlinks` and `resolve_links` implement Obsidian's own rules, not filename
matching: an exact vault-relative path wins, then a path relative to the linking
note's folder, then a unique basename. A **bare** `[[Name]]` with several matches
is ranked by proximity (same folder first, then shortest path) and returns
`ambiguous_candidates` listing the alternatives — check that field before
treating a result as certain.

## Exposing it to Claude

claude.ai and the mobile apps need a public **HTTPS URL on port 443**; a
non-standard port is rejected by claude.ai's connector validator ("Couldn't
reach this address") even when it is publicly reachable. Any HTTPS reverse
proxy works. With Tailscale:

```bash
tailscale funnel --bg 8780        # https://<your-host>.<your-tailnet>.ts.net
```

A Tailscale node has exactly one port 443, so running this beside another
public service on the same machine means giving it **its own node** — e.g. a
`tailscale/tailscale` sidecar container with `network_mode: service:obsidian-mcp`,
`TS_USERSPACE=true`, and a `serve.json` proxying to `127.0.0.1:8780`. The auth
key must be **reusable** and **non-ephemeral**, or the node vanishes on restart
and takes its hostname and certificate with it. Recreate the two containers
together: the sidecar shares the server's network namespace, and recreating one
alone orphans the other.

`OBSIDIAN_MCP_PUBLIC_URL` must equal the public URL exactly — it becomes the OAuth
`issuer`, and a stale value breaks discovery silently.

Three endpoints are unauthenticated by necessity: `/healthz`, the two
`/.well-known/oauth-*` documents, and `/register`. The login page is the only
thing guarding the vault, which is why it is password **and** TOTP.

## Authentication

OAuth 2.1, and only OAuth. It is the sole scheme because it is the only one
every client can use: Claude Code and Desktop can send headers, but claude.ai
and mobile connectors run OAuth discovery and have no field for one. A second
static-token path would have reached no client that this does not.

The OAuth server is shared in design with [anki-mcp](https://github.com/JasonSooter/anki-mcp) — same flow,
same threat model, one user each. The MCP server is itself the authorization
server; there is no upstream IdP because there is exactly one user:

```
Claude ──/authorize──► this server ──redirect──► /login (password + TOTP)
                                                    │
Claude ◄──?code=…──────────────────────────────────┘
  │
  └──/token──► access token (1h) + refresh token (30d)
```

The server refuses to start without both login factors. That is deliberate: the
login page is the only thing between the public internet and the vault, so a
half-configured deploy should fail at boot rather than serve with weaker auth.

Protections on the login page: constant-time comparison, 5 failures per 15 min
per IP keyed on the nearest trusted proxy hop, TOTP replay rejection (the
accepted counter is remembered), ±1 step of clock skew, and errors that never
reveal which factor was wrong. With telemetry on, every attempt is logged as an
`oauth login` event with the client IP — never the submitted secrets.

## Connecting a Claude client

One URL for everything: `https://<your-host>.<your-tailnet>.ts.net/mcp`. Every
client runs the OAuth flow itself — no token is pasted anywhere.

**Claude Code** (user scope):
```bash
claude mcp add --scope user --transport http obsidian \
  https://<your-host>.<your-tailnet>.ts.net/mcp
```
Claude Code opens a browser for the login on first use.

**Claude Desktop** — `claude_desktop_config.json`:
```json
"obsidian": {
  "command": "npx",
  "args": ["mcp-remote@latest", "https://<your-host>.<your-tailnet>.ts.net/mcp"]
}
```
`mcp-remote` performs the OAuth dance when no `--header` is given. If Desktop
can't find `npx` (GUI apps don't inherit an nvm `PATH`), use an absolute path.
Restart Desktop after editing.

**claude.ai and mobile** — Settings → Connectors → Add custom connector, paste
the same URL. Claude registers itself via `/register` (dynamic client
registration), then sends you to the login page. On iOS the TOTP field is marked
`autocomplete="one-time-code"`, so password managers offer the code directly.

## Quartz publishing (optional)

Publishes notes flagged `publish: true` to your own [Quartz](https://quartz.jzhao.xyz)
site repo, which your host (Vercel, Netlify, GitHub Pages...) builds on push.
Off unless `QUARTZ_ENABLED=true`.

### Two publishers, neither the owner

The site's `content/` directory is written by **both** this server and the
[Quartz Syncer](https://github.com/saberzero1/quartz-syncer) Obsidian plugin,
and that constraint drives the whole design. Syncer runs inside Obsidian (so it
can compile Dataview, but needs the app open); this server runs headless (so it
works from a phone, but cannot).

They format differently and **cannot be made byte-identical** — Syncer parses
every note to an mdast tree and re-serialises it through
`mdast-util-to-markdown`, so its output reflects that serializer's escaping,
list indentation, blank-line and emphasis rules. Reproducing that would mean
running the same serializer, in Node, pinned to the same options, and
re-breaking on every Syncer release.

So parity is not the goal. **The goal is that neither publisher rewrites the
other's files.** That is achieved by never asking "do these bytes match?" and
instead asking:

> Has this note been edited since it was last published?

`Publisher.published_at()` reads the last commit time of each file under
`content/`, and a note is republished only if its vault mtime is newer (plus a
15-minute grace window, because the plugin writes the file and commits a moment
later). Whoever published last set that timestamp, whatever formatting they
used. This took spurious updates from **36 to 0**.

Two settings *are* matched, because they are one line each and the most visible
difference: `emphasis: "_"` and `bullet: "-"`. Measured honestly, they close
about 3% of the remaining formatting gap — worth having, not worth mistaking for
progress toward parity.

### What is skipped

Notes containing ```` ```dataview ````/```` ```datacore ```` blocks are **not
published by this server** and are listed under `skipped` in the result. Syncer
executes those through Dataview's own API (`tryQueryMarkdown`, `executeJs`) and
inlines the result; that API is bound to Obsidian's `app`, `vault` and
`metadataCache`, so there is nothing to call here. Publishing them would replace
generated content with the raw query — visible breakage on the live page.

```` ```query ```` blocks are *not* skipped: those are Obsidian core search
blocks, which Syncer passes through verbatim too.

### Setup

1. **Deploy key**, scoped to the site repo only — never a personal access token:
   ```bash
   mkdir -p secrets
   ssh-keygen -t ed25519 -N "" -f secrets/quartz_deploy_key
   ssh-keyscan github.com > state/known_hosts
   ```
   Add the `.pub` at **Settings → Deploy keys → Add**, with **Allow write
   access** ticked. Without that tick the clone succeeds and only the *push*
   fails, which looks like everything working until it doesn't. Mount the key
   and set `QUARTZ_SSH_KEY_PATH` to its path inside the container.

2. **Clone the site repo** into the directory mounted at `/quartz`, then set
   `QUARTZ_ENABLED=true`.

3. **No Node in the image.** Set `QUARTZ_BUILD_LOCALLY=false` and let your host
   build on push. The trade-off is explicit: a local build is a gate that stops a
   broken note from shipping; the host's build catches it after the push instead.

### Publishing

```
list_published        what is flagged
publish_site()        dry run: stage, diff, push nothing   <- always first
publish_site(dry_run=False)
```

`publish_site` is the only irreversible action this server has.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `publish_site` reports dozens of updates with no edits | The clone is stale. `git pull` it — `published_at()` compares against local commit times. |
| Push fails, clone worked | Deploy key added without **Allow write access**. |
| Public URL times out; container looks healthy | A Tailscale sidecar sharing the server's network namespace was recreated alone. Recreate both together. |
| 502 from the MCP proxy | **Check `docker logs obsidian-mcp` for `POST /mcp` first.** If the request never arrived, the failure is between Anthropic's proxy and your public ingress, not your server — confirm with `curl https://<your-host>/healthz`, which will still answer. Affects every tool, not just slow ones. |
| A note is never published | It contains a Dataview block — check `skipped` in the result. |
| Frontmatter dates keep changing | Fixed: PyYAML was coercing `2024-06-04T19:23:37-06:00` into a datetime and re-emitting it with a space. Loader *and* dumper now leave date-like scalars alone. |

## Telemetry (optional)

Set `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` to ship
structured logs over OTLP — Grafana Cloud's OTLP setup wizard (Connections →
Add new connection → OpenTelemetry) generates both. Every attribute is queryable
as structured metadata in Loki:

```logql
{service_name="obsidian-mcp", deployment_environment_name="production"}
  | tool="vault_append" | status="error"
```

The endpoint must be the **base** `.../otlp` URL, not `.../otlp/v1/logs`: the
Python SDK appends the signal path itself. The header stays percent-encoded
(`Basic%20...`) — the SDK decodes it per the OTel spec.

**With no endpoint set, telemetry is a no-op** and the server logs to stdout —
an unreachable observability backend must never stop the vault being served.

`dashboards/obsidian-mcp.json` is a ready-made Grafana dashboard: import it via
Dashboards → New → Import. It expects a Loki datasource with uid
`grafanacloud-logs` (Grafana Cloud's default) and has an `$env` variable for
`deployment_environment_name`. Two things worth knowing if you edit it:

- `unwrap` works even though OTLP delivers every attribute as a string.
- `last_over_time(… | unwrap …)` returns **one series per container instance**,
  so a stat panel shows "N series" rather than a number. Aggregate it:
  `max by (vault) (last_over_time(…))`. Both stat panels here do.

### Events

| Body | When | Key attributes |
|---|---|---|
| `tool call` | every tool invocation | `tool`, `status`, `error`, `duration_ms`, `mutation`, `vault` |
| `vault mutation` | every write, via the journal | `tool`, `path`, `version_id`, plus per-tool fields |
| `auth failure` | a token that did not verify | `reason` (never any part of the token) |
| `server started` | boot | `vaults`, `quartz_enabled`, `port` |
| `vault stats` | boot | `notes` |

Two conventions the dashboard depends on, both pinned by tests in
`tests/test_core.py`:

- **Attributes are scalars only.** A list flattens into an unqueryable string, so
  `journal.record` drops non-scalars from telemetry while still writing them to
  the audit file. Tools that record a list also record a `*_count` — that is what
  the panels read.
- **Booleans are stringified** (`mutation="true"`), because LogQL structured
  metadata filters compare strings.

### Attribute names are a contract

Beyond scalars-and-strings, attribute keys must not collide with Python's
`logging.LogRecord` fields (`created`, `module`, `filename`, `name`, ...).
`journal.record()` forwards each tool's fields straight into a log record, so a
collision raises out of the *tool*, not the logger — enabling telemetry once
broke `vault_append` outright because it recorded `created`. That field is now
`note_created`, `emit()` renames any reserved key to `<key>_attr`, and `emit()`
can no longer raise at all. All three are covered by tests.

Instrumentation is a `@instrumented(...)` decorator applied under `@mcp.tool`. It
uses `functools.wraps` so the SDK still generates each tool's JSON schema from
the real signature — there is a test for that specifically, because losing it
would empty every tool's parameters on the wire without any other symptom.

### What the panels answer

- **Is it healthy?** — call volume, error rate, p95 latency by tool, restarts,
  vault size. Restarts usually mean the vault mount or `/state` went away.
- **Security and audit** — auth failures, rejected paths (traversal, symlinks,
  `.obsidian/`), and destructive/outward-facing calls. With one public URL and
  one login page, this row is the reason the dashboard exists.
- **Usage** — which tools actually get called, and errors by kind.
- **Vault activity** — mutations over time, capture volume, most-changed notes.
  Every line carries the `version_id` needed to undo it with `restore_version`.
- **Publishing** — real publishes (dry runs never reach the journal), notes added,
  build failures, and how often the undo journal was needed.

Note that a non-zero error count is not automatically bad: a refused traversal
and a refused stale write both count as errors and are the server working.

## Operations

- **Logs**: `docker compose logs -f obsidian-mcp`
- **What changed and when**: `tail -f state/audit.log` (JSON lines)
- **Recover a note**: `list_versions` then `restore_version`, or by hand from
  `state/versions/<vault>/<path>/`
- **Journal retention**: everything for 30 days, then thinned to one snapshot per
  day (`OBSIDIAN_JOURNAL_RETENTION_DAYS`). It is an undo buffer, **not a backup**
  — same disk as the vault, and your sync tool does not replicate it.

The server refuses to start if the vault is missing or `/state` is unwritable.
That is deliberate: serving `vault_write` and `vault_delete` with no undo is exactly
the situation the journal exists to prevent.

## Development

```bash
uv sync                 # Python 3.12, from .python-version and uv.lock
uv run pytest
docker build -t obsidian-mcp --build-arg GIT_REVISION=$(git rev-parse HEAD) .
```

Tests cover path confinement (traversal, symlink-out, symlinked parent, extension
allowlist), Obsidian link resolution including the ambiguous-basename case,
frontmatter-preserving appends and heading targeting, vault_patch's heading /
nested-heading / block / frontmatter targeting, the link rewriting behind
vault_move, the journal's snapshot/restore and version-id confinement, OAuth
hardening, and telemetry conventions.

The search tools and `list_backlinks` shell out to `ripgrep`, which the image
installs; running the server outside Docker needs `rg` on `PATH`.

CI runs the tests and builds the image on every pull request, and publishes
`ghcr.io/jasonsooter/obsidian-mcp` on every push to `main`. Dependencies are
kept current by Renovate.

## License

[MIT](LICENSE)
