# Local verification — 2026-10-28/29

Implemented after Astra's conditional-go architecture review. This record covers
local software and the Mac-first installation, not a homelab/domain deployment.

- `pytest -q`: **44 passed**. Includes Overcast snapshot/baseline/backfill,
  transaction rollback, concurrent claim, expired lease, bounded retry,
  lost-response receipt replay, FTS timestamps/status/title behavior, restoration,
  token/CSRF/Host separation, public projection and revocation, audio SSRF and
  streamed bounds, result spooling, and the passwordless QR session lifecycle, including final same-origin
  redirects and rejection of external authentication redirects.
- Real MCP 2.3 SDK client initialized over HTTP, listed all six read-only tools,
  searched, read a transcript page, and listed resource templates. Wrong owner,
  worker and missing credentials were rejected at the MCP boundary.
- The installed local service also passed a real SDK initialization, tool listing,
  and authenticated `pipeline_status` call using its actual MCP reader key.
- Real audio download -> local MLX Whisper -> worker completion API -> persisted
  transcript -> FTS query passed in an isolated temporary database. Source was the
  public-domain JFK test audio in OpenAI Whisper's repository. Default model:
  `mlx-community/whisper-small-mlx`. Measured **17.42 seconds**, including first
  model acquisition; child peak RSS **1183.2 MiB**. One segment/22 words; the result
  spool was empty after its durable receipt. This is a short functional smoke
  check, not an hour-long podcast performance or transcription-quality benchmark.
- `uv build` produced wheel/sdist; wheel includes seven templates and two static
  assets. `docker compose ... --profile sharing config --quiet` passed. No local
  Docker daemon was available, so the image was not built/run here.
- Chrome browser checks: real sign-in, library search, precise passage navigation,
  in-page highlighting, desktop layout, and 390px phone layout. Both library and
  reader had `scrollWidth == innerWidth == 390`, with no horizontal overflow.
  Browser testing caught the HTTP no-referrer/Origin interaction; private pages
  now use same-origin referrer policy while public pages retain no-referrer.
- Local server and worker LaunchAgents are installed and running. Private app
  listens at **127.0.0.1:8765**. Worker uses a separate 0600 `.env.worker`; server
  configuration, data directory and SQLite file have restrictive permissions.
  The separate fictional-content demo was used for UI inspection.

## Still requires real integration

- Phone approval for the real Overcast session, followed by actual inventory
  discovery and one real listened-episode transcription.
- Homelab placement, private custom-domain DNS/TLS/Tailscale ingress and rejection
  from outside the tailnet. No DNS/proxy/ACL changes have been made.
- Optional public share hostname deployment. Synthetic publish/revoke and public
  route isolation pass locally; no personal content has been made public.
- Real Metas Muse network path and search/read round trip. The user confirmed
  service URL + bearer token support. Actual Muse connectivity is not yet proven.

The live source and client steps above must not be reported as completed merely
because local fixtures, the generic MCP client or deployment examples pass.
