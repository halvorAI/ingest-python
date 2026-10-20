# Changelog

## 0.1.0 (developer preview)

- `HalvorClient`: batched, signed (EdDSA) event delivery; `emit` never raises.
- `agent_context`: attributes payments to an agent, owner and instruction; instruction text is hashed locally.
- Offline mode: pass `sink` to receive signed envelopes without an account; `generate_source_key` makes a signing key.
- `sign_event` / `verify_event`: detached JWS over the JCS form, byte-identical to the Node.js SDK.
- x402: `wrap_httpx` observes payment requirements, payloads and settlement responses.
- Events follow [APE v0.1](https://schema.halvorai.com).
