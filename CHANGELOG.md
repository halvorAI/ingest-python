# Changelog

## Unreleased

### Added

- `halvorai-ingest init` (also `python -m halvorai_ingest init`) and `run_init()`: generate a source key, register it with `POST /v1/sources`, write `.env.local` and send a test event, as `npx @halvorai/ingest init` does. Python users no longer need Node.js to connect to HalvorAI.

## 0.2.0 (unreleased)

### Breaking

- `emit` hands events to a background thread and returns at once. Delivery happens every `flush_interval` seconds (default 1), when a batch is full, on `flush()`, on `close()` and at interpreter exit. Call `flush(timeout=…)` before a short-lived process or serverless handler returns.
- `flush(timeout=None, *, replay_spool=True)` returns `{"drained": bool}`.
- x402 events without a network or token address raise inside the mapper and are reported as `invalid_event`, instead of being recorded with a guessed testnet, zero address, `USDC` or 6 decimals.
- A source key that is not an Ed25519 private JWK with a `kid`, or an API key that is not `hk_test_…`/`hk_live_…`, disables the client with one `config` error at construction.
- `validate_event` rejects `null` anywhere in an event, as APE v0.1 does.

### Fixed

- Retries with backoff (`retry_delays`, default 1, 2, 4, 8, 16, 32 s) on network errors, 408, 425, 429 and 5xx. 5xx and network failures no longer count as `rejected`.
- Permanent errors (400, 401, 403, 404, 405, 409, 410, 415, 422) are not retried; the batch goes to `<spool_dir>/dead/` and is reported once with `serverCode`, `details` and `requestId`.
- A batch that is too large (413) is split; a single oversized event is dropped and reported.
- x402: `payment.requested` has `payment_id` `x402:<network>:requested:<event id>`; authorization, settlement and failure share `x402:<network>:<nonce>` and never use the transaction hash; `payment.authorized` links to its challenge with `related.previous_event_id`; a failed settlement has no `tx_hash` and a `failure_reason` of at most 128 characters; the payer wallet is the instrument. Output matches the Node.js SDK for the same headers.
- x402 events keep the full agent and owner from `agent_context`.
- An event-level `instruction` replaces the context one, and only APE instruction fields are kept.
- Card numbers are redacted in `payee.website`, `payee.resource_url`, label keys and JSON evidence (the evidence hash is recomputed), and rejected there by validation.
- JCS: strings with a lone UTF-16 surrogate are written as `\udXXX`, like the Node.js SDK, instead of raising.
- `stats()` returns a copy, and counters are exact under concurrent `emit` calls.

### Added

- Disk spool (`spool_dir`, `spool_max_bytes` default 50 MB, `spool_max_age` default 7 days): undeliverable batches are kept and replayed by the next `flush` or process; the oldest files are evicted first and reported.
- `close(timeout)`, context manager support, and an `atexit` flush (`exit_hook=True`, 2 s).
- `stats()` has `dropped`, `retries`, `spooled` and `replayed`.
- x402: `HalvorX402Transport` and `AsyncHalvorX402Transport` (see every request, including `stream()`), `wrap_httpx_async`, and every HTTP method on the wrappers.
- Tests run the ape-schema conformance cases (40 events, JCS vectors).

### Changed

- `emit` validates every event against APE v0.1 before signing; an invalid event is dropped, counted in `stats().dropped_invalid` and reported through `on_error` with `invalid_event`.
- The default endpoint follows the key: `hk_test_` keys (and `environment="test"` with no key) go to the sandbox, `hk_live_` keys to production.
- `endpoint` is a base URL, as in the Node.js SDK; a full `/v1/events` URL still works. `HALVOR_ENDPOINT` sets it from the environment.
- `HalvorClient()` reads `HALVOR_API_KEY` and `HALVOR_SOURCE_KEY` from the environment.
- Delivery counts each event by the status the API returned: `accepted`, `duplicates` and `rejected`.
- `transport` accepts an `httpx` transport, for tests.
- The SDK version in `source.sdk` comes from the installed package metadata.

## 0.1.0 (developer preview)

- `HalvorClient`: batched, signed (EdDSA) event delivery; `emit` never raises.
- `agent_context`: attributes payments to an agent, owner and instruction; instruction text is hashed locally.
- Offline mode: pass `sink` to receive signed envelopes without an account; `generate_source_key` makes a signing key.
- `sign_event` / `verify_event`: detached JWS over the JCS form, byte-identical to the Node.js SDK.
- x402: `wrap_httpx` observes payment requirements, payloads and settlement responses.
- Events follow [APE v0.1](https://schema.halvorai.com).
