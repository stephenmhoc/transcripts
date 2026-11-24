# Astra architecture review

Reviewed 2026-10-28, before application implementation. Scope: the proposed
architecture in `docs/architecture.md`, using the supplied Overcast connector
findings. This is a design review, not verification of a deployed system or of
Metas Muse connectivity.

**Recommendation: conditional GO for implementation.** The single ASGI service,
SQLite, HTTP worker, and local MLX transcription are a sensible small system.
Capture the requirements below before building their respective components. A
working local application is achievable without resolving public hosting first.
Production deployment and a claim of working Muse integration remain gated by
real connectivity, credentials, and end-to-end tests.

## Required design corrections

### 1. Define import baselines and eligibility precisely

The proposed seven-day initial import is good, but `userUpdatedDate` is a state
change, so it cannot establish when listening happened. Present this as “recent
Overcast activity,” not “listened in the last seven days.” The available export
is a snapshot of retained account state, not a guaranteed complete listening log.
Missing episodes must not delete existing transcripts or imply they were unplayed.

Persist the first successful baseline transactionally with its inventory. At that
baseline, queue qualifying episodes only when a valid activity timestamp falls
within the initial window. Unknown, malformed, or implausibly future dates must
be held for explicit backfill, not silently treated as recent. A failed first
import must not advance the baseline. Thereafter, distinguish newly observed
episodes and qualifying state transitions from unchanged historic entries. A
repeated export, metadata change, process restart, or unplay/replay cycle must not
create another transcription for an already completed episode. Use a database
uniqueness constraint for active work and require an explicit action to retranscribe.

Use the Overcast episode ID as the source-specific key. Define what happens when
it is absent: quarantine/report the entry or use a documented feed-plus-GUID
fallback. Do not use a signed enclosure URL as stable identity. Backfill should
show the count, enforce a bound, and be explicit about the selection rule.

### 2. Treat every downloaded URL as untrusted input

“Redirect checks” needs an enforceable rule. The worker can reach both the public
Internet and the tailnet, so a malicious feed/enclosure could otherwise turn it
into a probe of local services. Permit only HTTP(S); reject userinfo, loopback,
private, link-local, multicast, unspecified, and tailnet destinations for audio
downloads. Revalidate each redirect and the address actually used for the
connection; hostname checking alone leaves DNS rebinding gaps. Keep the worker's
configured control-server connection separate from the audio-fetch policy. Apply
streamed byte limits, redirect limits, timeouts, and available-disk checks before
inference. Never forward the Overcast cookie or worker token to an enclosure host.

Keep signed URLs exact in storage and requests but redact query strings and
credentials from logs, error messages, public output, and discovery diagnostics.
Validate model identifiers against configured models and never turn server job
fields into executable shell commands or arbitrary filesystem paths.

### 3. Specify the queue's completion transaction

A lease plus heartbeat is not sufficient without the exact completion behavior.
Claim atomically; fence heartbeat, failure, and completion by the current attempt
token; and reject stale workers after expiry/reassignment. Heartbeat independently
of blocking inference. Commit transcript metadata, segments, FTS changes, and job
completion together. A replay of the same accepted result must return a successful
receipt; a different or stale result must not replace the completed transcript.
This covers the important case where the server commits and the response is lost.

Use server time for lease decisions. Persist retries and next-attempt time. Test
the worker dying during download and inference, the server restarting, a lost
completion response, a late upload after lease replacement, and a prolonged
network outage. Keep spooled results private and bounded, and remove audio only
after the durable receipt. Define cleanup for exhausted jobs and abandoned files
so outages cannot fill the mini's disk indefinitely.

Run the embedded discovery scheduler in exactly one application process in v1.
Multiple ASGI workers must not silently create multiple pollers; document and
enforce that deployment constraint rather than adding another queue service.

### 4. Make public sharing a deliberate privacy boundary

The separate public listener is a strong design choice. Credential-looking feed
URLs are only a warning heuristic: premium/private content can have ordinary
looking URLs, and enclosure credentials may differ from feed credentials. Require
explicit feed-level permission before a feed's first public share. Once enabled,
the requested one-click per-episode publish/revoke flow is straightforward.

Publish only a deliberately selected projection of fields, ideally a separate
public-share store containing the rendered/sanitized content and opaque share
token. Do not give the public process Overcast secrets, owner tokens, or an
unrestricted private database connection. Escape transcript and publisher text;
any accepted HTML needs sanitization. Use cryptographically random share IDs and
exclude external images/audio by default, since they can disclose reader visits.
Prevent cache persistence and ensure revocation is checked on every public read.

Explain that a share link is available to anyone who receives it. `noindex` is a
request to crawlers, not authentication, and revocation cannot remove copies
already downloaded. Do not expose a share control as successful until the public
hostname/listener is configured and reachable. A distinct share hostname is a
reasonable implementation of the user-facing privacy requirement.

### 5. Separate build readiness from integration readiness

The private domain needs an actual tailnet route, resolver arrangement, and valid
certificate; a Compose file cannot prove these exist. Confirm the homelab target,
reverse proxy, DNS authority, storage path, and backup destination before changing
deployment. Do not bind the private app to all interfaces on the assumption that
the proxy or a container network makes it private. Verify access from an authorized
tailnet client and rejection from the public network.

The pending Muse question is consequential: both its authentication scheme and
the network location of its MCP client matter. A cloud-hosted client cannot be
assumed to reach a private tailnet endpoint. Implement bearer-authenticated,
read-only MCP as the initial supported contract, but do not describe Muse as
connected until a real Muse search/read round trip succeeds. If Muse requires
OAuth or public reachability, resolve that explicitly rather than exposing the
owner service or quietly weakening the private default.

Use separate random owner, worker, MCP, and session-signing secrets; Secure,
HttpOnly, SameSite browser cookies; CSRF checks on cookie-authenticated writes;
and no tokens in URLs. Validate MCP Host/Origin as appropriate for the supported
client and prevent bearer tokens from crossing redirects. Document token rotation
and restrict secret/database/backup file permissions. Backups include listening
history and possibly signed URLs, so treat them as private material too.

## Keep the first version small

Do not add PostgreSQL, Redis, an agent framework, automatic summaries, or OAuth
until a demonstrated requirement needs them. Start with one owner and one mini
worker. Use a conventional server-rendered UI unless a concrete interaction needs
more. One status page can cover discovery failures, expired Overcast auth, queue
failures, and worker freshness. Use an explicit manual OPML import to exercise the
same parser and scheduling path before credentials are available.

Store the transcription model, relevant decoding options, software version, audio
checksum when available, and completion time with each transcript. Label machine
transcripts and let users follow timestamps without promising word-level accuracy.
Initial model acquisition needs network access and disk space even though the
audio inference itself is local. Verify one representative real transcription on
this mini rather than inferring runtime compatibility from installation alone.

## Acceptance gates

1. Local build: parser fixtures and first-run/repeated-import tests; durable queue
   failure/replay tests; role and CSRF isolation; transcript/FTS consistency;
   genuine MCP SDK handshake plus tool calls; desktop/mobile UI inspection.
2. Local ML: a real audio download/transcription/upload/read round trip, with
   resource use and any model-download prerequisite reported honestly.
3. Homelab private deployment: restart persistence, actual tailnet HTTPS access,
   public rejection, worker recovery, and a restored backup exercised against a
   temporary database. Record any still-unconfigured credentials or services.
4. Optional sharing: public route allowlist and field projection verified,
   unrelated private routes unavailable, publish/revoke tested with synthetic
   content, and no personal transcript published without user authorization.
5. Muse: authenticated search and paginated reading from the real client. A sample
   configuration or a generic MCP client test alone does not satisfy this gate.

No fundamental redesign is needed. The main risks are misleading discovery
semantics, worker access to internal networks, duplicate/lost results during
outages, accidental public exposure, and overstating deployment readiness. The
requirements above make those risks concrete and testable without expanding the
system substantially.
