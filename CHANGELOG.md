# Changelog

## 0.2.0 (unreleased)

- `emit` validates every event against APE v0.1 before signing; an invalid event is dropped, counted in `stats().dropped_invalid` and reported through `on_error` with `invalid_event` and the problems found, as in the Node.js SDK. Previously it was signed and sent.
- The default endpoint follows the key: `hk_test_` keys (and `environment="test"` with no key) go to the sandbox, `hk_live_` keys to production. Previously every key went to production.
- `endpoint` is now a base URL, as in the Node.js SDK (`https://api.sandbox.halvorai.com`); a full `/v1/events` URL still works. `HALVOR_ENDPOINT` sets it from the environment.
- `HalvorClient()` reads `HALVOR_API_KEY` and `HALVOR_SOURCE_KEY` from the environment when they are not passed.
- Delivery counts each event by the status the API returned: `stats()` has `accepted`, `duplicates` and `rejected`, and each rejected event is reported through `on_error` with its `serverCode`. Events that could not be sent for lack of an API key count as `dropped_disabled`.
- `transport` accepts an `httpx` transport, for tests.

## 0.1.0 (developer preview)

- `HalvorClient`: batched, signed (EdDSA) event delivery; `emit` never raises.
- `agent_context`: attributes payments to an agent, owner and instruction; instruction text is hashed locally.
- Offline mode: pass `sink` to receive signed envelopes without an account; `generate_source_key` makes a signing key.
- `sign_event` / `verify_event`: detached JWS over the JCS form, byte-identical to the Node.js SDK.
- x402: `wrap_httpx` observes payment requirements, payloads and settlement responses.
- Events follow [APE v0.1](https://schema.halvorai.com).
