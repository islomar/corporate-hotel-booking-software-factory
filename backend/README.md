<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Backend](#backend)
  - [Request limits](#request-limits)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Backend

FastAPI app in `src/chb`. Run the checks with `.devcontainer/run make check` from the repository root.
The root [README](../README.md) covers setup, observability and the sandbox container.

## Request limits

`src/chb/request_limits.py` protects the in-memory repositories from one oversized request or a flood (CHB-23).
Both limits are settings read from environment variables at start. An invalid value stops the start with an error that names the variable.

| Variable | Default | Meaning |
|---|---|---|
| `MAX_REQUEST_BODY_BYTES` | `65536` (64 KiB) | A body over this size gets `413`. At least 1. |
| `RATE_LIMIT_WRITES_PER_MINUTE` | `60` | Write requests (POST, PUT, PATCH, DELETE) per client IP per minute. The next one gets `429` with a `Retry-After` header in seconds. At least 1. GET requests are not limited. |
| `TRUSTED_PROXY_HOPS` | `1` | How many proxies in front of the app append to `X-Forwarded-For`. `1` is Cloud Run. `0` ignores the header and uses the socket address. |

How it works:

- 413: the app refuses a `Content-Length` over the limit before the route runs.
  It counts a body without that header (chunked) as it arrives and stops reading at the limit.
  It never holds more than the limit plus one chunk.
- 429: a fixed window of 60 seconds per client IP, kept in this process.
  A client can send up to twice the limit across a window edge.
  The table holds at most 10,000 clients and drops the oldest when full.
- Cloud Run runs one instance (ADR-d5e6), so the counts are exact. With more instances each one counts alone.
- The client IP is the entry at position `TRUSTED_PROXY_HOPS` from the right of `X-Forwarded-For`, because Cloud Run appends the caller's address there.
  The client writes the entries to the left, so the app ignores them.
  If the app runs without a proxy that appends the header, set `TRUSTED_PROXY_HOPS=0`.
  Otherwise a client can forge the header and dodge the limit.
- Both answers carry the security headers and a JSON body `{"detail": "..."}`.
