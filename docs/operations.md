# Operating Transcripts

## Boundary and secrets

One ASGI application process owns the private SQLite file and embedded discovery
loop. Do not run multiple ASGI workers. A process lock rejects a second scheduler;
a separate sync lock prevents a CLI sync and web poll from running together.
One mini worker pulls one lease at a time; a local process lock prevents duplicate
inference. Both processes recover their queue state across restarts.

Generate four separate random secrets via `transcripts init`: owner browser/API,
read-only MCP, worker, and session signing. Keep `.env`, `overcast.json`, databases,
spools and backups private (0700 directories, 0600 files). Never put bearer tokens
or source audio URLs in URL query parameters, issue descriptions, or logs. Passwords
are not persisted. Browser sessions are HttpOnly/SameSite and Secure on HTTPS;
rotating either the owner key or session secret invalidates existing sessions.
The exact `APP_ORIGIN` is required by Host/Origin checks; forwarded headers are not
trusted. The private service intentionally uses `Referrer-Policy: same-origin` so
normal HTTP loopback forms retain their Origin. Public shares use no-referrer.

## Local development

The checked-out mini can run everything with `.env` and loopback URLs. This is not
an installation at the final custom domain. Use a normal browser; the standalone
site needs same-origin form and cookie behavior. The demo uses a separate database
and configuration, is clearly labeled, and must never receive personal credentials.

Create the worker's own environment file once it runs remotely. It needs only:

```text
WORKER_URL=https://transcripts.merimerimeri.com
WORKER_TOKEN=<the server's separate worker token>
WHISPER_MODEL=mlx-community/whisper-large-v3-turbo-q4
```

The worker must not receive owner/MCP/session secrets or an Overcast cookie.

## Server deployment

`deploy/compose.yaml` runs the private app (and, under the `sharing` profile, the
public share listener) with ports published only on host loopback. Put a TLS
reverse proxy in front that binds only to a private interface, such as a Tailscale
address, and never proxy the private app to a public listener. Create a fresh
private `.env` beside the Compose file with `APP_ORIGIN` set to the exact HTTPS
origin, then verify owner sign-in, MCP and worker auth from an allowed client and
confirm the private hostname is unreachable from outside.

The Docker image uses the locked server dependency set, without MLX/torch. Its
private container starts with one Uvicorn worker. Access logs are disabled to avoid
recording session-related URLs. Docker log rotation is bounded in Compose.

## Install the Mac mini worker

Install `ffmpeg`, then `uv sync --extra worker`. Create the worker-only env file
(mode 0600). With the verified reachable private origin, install the LaunchAgent:

```sh
.venv/bin/python scripts/install_launchd.py worker \
  --env /absolute/path/to/worker.env \
  --url https://transcripts.merimerimeri.com
```

The installer writes a plist containing paths and arguments only; secrets stay in
the env file. It refuses to overwrite an existing agent. It sets a minimal PATH
including Homebrew's ffmpeg. Inspect with:

```sh
launchctl print gui/$(id -u)/com.merimerimeri.transcripts.worker
tail -n 50 var/logs/worker.err.log
```

KeepAlive restarts crashes, with a 30-second throttle. A LaunchAgent requires a
logged-in user session; FileVault/reboot and sleep behavior must be considered for
unattended use. Logs are under `var/logs`; rotate these with normal host log policy.
A server LaunchAgent is also available for an explicitly chosen mini-hosted phase;
do not leave it running after moving discovery/server duties to another host.

## Queue recovery and resource bounds

Lease length is 15 minutes; heartbeats run independently every minute. Each attempt
has a random token. Expired heartbeats, completion, and failure are rejected after
replacement; claims are atomic. Only one active job is permitted. Failures back off
persistently (two then four minutes) and stop after three attempts; retry manually
from the episode page. A crashed worker's lease is recovered on the next claim.

The result and receipt commit transcript metadata, segments, FTS index and job
completion in one transaction. Replaying the exact accepted result returns the
same receipt. A different result cannot replace it. Until receipt, a private local
result spool can retry after a server outage; a stale attempt becomes a `.stale.json`
file retained up to seven days. Pending/spooled result storage stops new work at
100 MB. Inspect before removing unsent results. A long outage can expire a lease
and require another transcription; stale workers cannot overwrite the accepted one.

Audio downloads pin validated globally routable IPs for HTTP(S) and validate each
redirect. No loopback/LAN/link-local/tailnet destinations, custom ports, userinfo,
or forwarded server credentials are allowed. Downloads are limited to 512 MiB,
30 minutes, 5 redirects and socket timeouts, with a 2 GiB disk reserve. Inference
runs in a separate killable process with a two-hour limit. Individual episodes
beyond those bounds fail visibly. The model is selected only by local config.

Temporary audio is removed after receipt, or after writing a durable text spool
when the network prevents receipt; abandoned audio from killed workers is cleaned
on restart under the process lock. The original audio is not a permanent archive.
SQLite writes never span download, inference or network I/O.

## Public sharing

By default the private app serves shared copies itself at `/p/<token>` (plus
`/p/public.css`). Those are the only paths that skip owner auth, and they read only
`data/public/shares.sqlite3`. When the private hostname sits behind Cloudflare
Access, create a second Access application scoped to the path
`transcripts.merimerimeri.com/p/` with one Bypass/Everyone policy. Verify from a
logged-out browser that `/p/<token>` loads and `/`, `/connections` and `/mcp` still
require Access.

Optional separate listener: run `docker compose --profile sharing up -d --build`
(or `transcripts public`), route a public hostname to it, and set
`PUBLIC_BASE_URL` to that origin. Publishing then probes its `/healthz` first.
Leave `PUBLIC_BASE_URL` empty to use the same-origin `/p/` paths.

Publish/revoke use one attached-database SQLite transaction in DELETE journal
mode. Both databases must be ordinary files on the same local disk. Revocation
cannot retract copies already saved.

## Backup and restore

The canonical private file is `data/library.sqlite3`. It uses SQLite's default
DELETE journal mode, a busy timeout, foreign keys, and short write transactions.
Use the SQLite online backup API; copying a live database is not a safe backup.

```sh
uv run transcripts backup /private/backup/library-2026-10-28.sqlite3
```

The command refuses an existing destination and verifies `integrity_check`.
Tests restore a backup into a new location and prove completed transcript reading,
FTS search, and no duplicate pending work. Backups contain listening history and
credential-bearing source URLs, so put them in encrypted backup storage, not a public bucket or Git.

For actual recovery: stop private and public services; preserve the existing data
folder; restore the private database into a **new** directory; check integrity;
clear all `episodes.share_token` values in the restored private database; and
initialize an **empty** public-share database before restarting. Do not restore an
old public-share database automatically, since that could resurrect revoked links.
Restore the owner/MCP/worker/session env secrets and Overcast cookie separately
from protected storage, or rotate/reconnect them. A local worker spool can then
retry results; stale attempts remain fenced. Recheck private reachability, live
Overcast sync, worker recovery, search and Muse after recovery.

## Maintenance and honest health

`/healthz` only proves the app/database respond. Pipeline distinguishes source auth,
last successful live Overcast fetch, worker last seen, queue failures and totals.
A file import is not a successful authenticated Overcast check. No future/malformed
date advances auto eligibility. A failed initial import does not advance baseline.

For secret rotation, generate a fresh random value, replace only the intended env
field, restart its consumers and reverify that the old key fails. Changing worker
or MCP keys requires updating those specific clients. Owner/session changes log
out browsers. Expired Overcast auth requires scanning a new QR code; it does not
require losing transcripts or clearing the baseline.
