# Transcripts — implementation sketch

A personal, searchable shelf of podcast transcripts. Priority: discover listened
Overcast episodes automatically, transcribe on the Mac mini, expose the collection
through authenticated MCP. The website favors quiet typography, useful search,
timestamped reading, and clear pipeline health over dashboards.

## Proposed first version

- Homelab: one Python ASGI application, SQLite (FTS5), durable queue, and an
  in-process discovery loop. No Redis, PostgreSQL, hosted transcription, or LLM
  summarization. The existing podcast-sync service is independent: subscription
  RSS is not evidence that an episode was listened to.
- Overcast: poll the authenticated `account/export_opml/extended` endpoint twice
  daily. Save only its session cookie, not the password. Also accept local OPML
  imports for recovery. Parse `played`, `progress`, `userUpdatedDate`, and stable
  Overcast IDs. Preserve signed enclosure URLs exactly. `played` can mean marked
  played; `userUpdatedDate` is a state-change time, not a precise listening date.
  Include episodes with >= 5 minutes of progress; finished/marked-played episodes
  qualify. Default initial import records the full inventory but queues only
  activity in the last 7 days, with explicit bounded backfill. Never enqueue an
  entire historic account silently. Surface expired auth and schema drift.
- Mac mini: a launchd worker pulls jobs over Tailscale HTTPS, downloads audio with
  bounded size/time and redirect checks, transcribes via MLX Whisper locally,
  posts timestamped segments. No inbound SSH shell endpoint or arbitrary commands.
  One worker/inference at a time. Work is leased, heartbeated, attempt-fenced,
  retried with backoff, and fails visibly after a bounded number of attempts.
  Delete audio after durable result receipt; spool results locally for outages.
- Storage: feeds, episodes, listening observations, discovery runs, jobs, transcript
  provenance/model/checksum, timestamped segments, FTS index, and sharing events.
  Stable Overcast episode IDs deduplicate repeated exports; completed transcripts
  survive metadata refreshes and job retries. Server alone opens the SQLite file;
  worker communicates over HTTP. SQLite online backup plus restore verification.
- UI: private library, full-text search with timestamped matches and podcast/status
  filters, episode reader, download text, queue/retry controls, discovery health,
  connection/setup page. No invented transcripts in the actual library; demo data
  lives in a separate optional database.
- Auth: fail closed without configured strong secrets. Separate owner, MCP-reader,
  and worker bearer tokens. Browser signs in once with the owner token and uses a
  signed HttpOnly cookie; CSRF protection for cookie mutations. MCP cannot publish
  or mutate the library. Start with read-only Streamable HTTP MCP: search, list
  podcasts/episodes, read paginated timestamped transcript, pipeline status.
  Provide a standard client config for Metas Muse, pending confirmation of its
  bearer-token support and ability to reach the tailnet. OAuth is a separate
  integration if required; do not claim universal OAuth compatibility.

## Privacy and sharing

The private listener is bound to loopback by default. On the homelab, bind its
reverse proxy only to the Tailscale interface. Split DNS maps
`transcripts.merimerimeri.com` to the tailnet IP; a custom-domain certificate needs
DNS-01 (Tailscale certificates cover the node's ts.net name, not this domain).
A bearer token is still required, as defense in depth and for MCP identity.

Optional public sharing uses a *separate application/listener* serving only
`/p/{random-share-id}`. Public proxy routes to this listener, never the private
one. Suggested public hostname: `share.transcripts.merimerimeri.com`. One click
publishes one ready transcript; a second revokes immediately and rotates the
share ID on future publication. Public responses use no-store/noindex and contain
only show/title/publication metadata and transcript, never signed audio/feed URLs,
listening history, internal IDs, or private search/navigation. The public listener
has no login, owner API, worker API, or MCP routes. It stays disabled until a
public base URL is configured. Initially sharing blocked for feeds whose URLs
appear credential-bearing; owner can explicitly permit a feed before publishing.

## Verification and delivery

Implement only after Astra reviews this plan. Test Overcast fixtures (actual
extended schema, missing fields, partial listening, dates, duplicates), lease
fencing/recovery/retries, FTS timestamps, revoked shares, role isolation, CSRF,
public route isolation, and real MCP SDK client handshake/tool calls. Run a local
MLX transcription and upload round trip if model/runtime resources permit. Inspect
the actual UI in the browser at desktop/mobile sizes. Supply Docker Compose,
private/public proxy examples, Mac launchd installation, backup commands, and
honest deployment/credential status. Do not change DNS, existing service routes,
or publicly publish personal transcripts as part of local verification.

## Sources inspected

- https://overcast.fm/ — standard RSS and direct publisher downloads.
- https://github.com/hbmartin/overcast-to-sqlite — primary connector code shows
  session cookie `o`, extended export endpoint, and actual OPML attribute names.
  This is an unofficial integration and can change.
- https://raw.githubusercontent.com/ml-explore/mlx-examples/main/whisper/README.md
- https://modelcontextprotocol.io/docs/develop/build-server
- https://tailscale.com/docs/how-to/set-up-https-certificates

## Accepted Astra corrections (before implementation)

See `astra-plan-review.md` for the independent review. Implement a transactional
first baseline with invalid/unknown dates held for bounded backfill, preserve
missing historical records, and distinguish activity from actual listen times.
Pin the public IP used for each audio connection and revalidate every redirect.
Complete metadata/segments/search/job in one transaction, with result receipts
and fencing on every worker operation. Use a process lock for discovery. Four
separate secrets cover owner, session signing, worker, and read-only MCP.

Sharing requires feed-level opt-in for *every* feed. Project the chosen content
into a separate public SQLite database; the public process gets only that file
and no secrets/private database. Use attached DELETE-journal databases for atomic
publish/revoke. Require an explicit sharing-enable setting and a successful
public listener health probe before publishing. Start local transcription with
MLX Whisper small (multilingual, configurable), and measure a real round trip. Muse's exact
client auth and network path remain integration requirements, not assumptions.
