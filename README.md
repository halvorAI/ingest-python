# halvorai-ingest

Signed, attributed payment events for AI agents. HalvorAI records each payment an agent makes, with the agent, the owner it acted for, the instruction it followed and the network proof that came with it. It is never in the payment path: it cannot slow or block a payment.

> **Developer preview.** The SDK works offline today: build, sign and inspect events with your own sink. Sandbox API keys go to early-access teams. [Request access](https://halvorai.com/?utm_source=github&utm_campaign=ingest_python#early-access).

- Docs: [docs.halvorai.com](https://docs.halvorai.com)
- Event format: [Agent Payment Event (APE) v0.1](https://schema.halvorai.com), open and Apache-2.0
- Python 3.10 or later

## Install

```bash
pip install halvorai-ingest
```

## Try it offline

No account needed. Events are signed with your own Ed25519 key and handed to your `sink` instead of the API.

```python
import json

from halvorai_ingest import HalvorClient, agent_context, generate_source_key

halvor = HalvorClient(
    source_key=generate_source_key(),  # private JWK; keep it secret
    environment="test",
    sink=lambda envelopes: print(json.dumps(envelopes, indent=2)),
)

with agent_context(
    agent={"id": "agt_travel_12", "name": "Travel booking agent"},
    owner={"id": "own_u_4410", "type": "person", "country": "US"},
    instruction={"id": "task_8890", "text": "Book a return flight to Austin under $500"},
):
    halvor.emit(
        {
            "type": "payment.settled",
            "payment": {
                "payment_id": "pi_3QxAbCdEfGhIjKlM",
                "rail": "stripe",
                "amount": {"value": "41200", "unit": "minor", "currency": "USD"},
                "status": "settled",
                "rail_ref": {"payment_intent": "pi_3QxAbCdEfGhIjKlM"},
            },
            "payee": {"type": "merchant", "name": "Example Airways"},
        }
    )

halvor.flush(timeout=5)
```

`emit` returns at once and never raises; a background thread signs and sends events. Instruction text is hashed on your machine and never sent. Anyone with the public half of the key (the JWK without `d`) can check an envelope with `verify_event(envelope["event"], envelope["sig"], public_key)`.

## Send to HalvorAI (early access)

With a sandbox key (`hk_test_…`), register this machine as a source. `init` creates an Ed25519 key here, registers its public half, writes `HALVOR_API_KEY` and `HALVOR_SOURCE_KEY` to `.env.local`, sends one test event and prints a dashboard link:

```bash
halvorai-ingest init            # or: python -m halvorai_ingest init
```

It asks for the key unless `--api-key` or `HALVOR_API_KEY` is set. Other options: `--endpoint`, `--name`, `--cwd`, `--dashboard`. The private key never leaves your machine; keep it in your secret manager.

Then load `.env.local` (or set the same variables) and create the client:

```python
import os

from halvorai_ingest import HalvorClient

halvor = HalvorClient(
    api_key=os.environ["HALVOR_API_KEY"],
    source_key=os.environ["HALVOR_SOURCE_KEY"],  # the private JWK as JSON
)
```

## Delivery

- Events are sent every `flush_interval` seconds (default 1), or sooner when a batch of `batch_size` is full.
- Network errors, 408, 425, 429 and 5xx are retried with backoff (`retry_delays`, default 1, 2, 4, 8, 16, 32 s). Permanent errors (400, 401, 403, 404, 405, 409, 410, 415, 422) are not retried; the batch is reported through `on_error` with the API's `serverCode` and `requestId`.
- Pass `spool_dir` to keep batches that could not be delivered on disk. The next `flush()` or process replays them. Rejected batches go to `<spool_dir>/dead/`. Limits: `spool_max_bytes` (50 MB) and `spool_max_age` (7 days), oldest first.
- `stats()` returns counts: `emitted`, `accepted`, `duplicates`, `rejected`, `dropped`, `retries`, `spooled`, `replayed`, and the `dropped_*` reasons.

Long-running processes need nothing else: queued events are flushed at exit (up to 2 s). In a short-lived script, a serverless handler or a worker task, flush before you return:

```python
result = halvor.flush(timeout=5)   # {"drained": True} when everything was handled
```

Or use the client as a context manager, which closes it on exit:

```python
with HalvorClient() as halvor:
    ...
```

## x402

Put the HalvorAI transport under the `httpx` client your agent pays with. Payment requirements, signed payment payloads and settlement responses become events. Headers are only read; nothing in the payment path changes.

```python
import httpx

from halvorai_ingest.x402 import AsyncHalvorX402Transport, HalvorX402Transport

client = httpx.Client(transport=HalvorX402Transport(halvor))
async_client = httpx.AsyncClient(transport=AsyncHalvorX402Transport(halvor))
```

To keep a custom transport, pass it as `inner`: `HalvorX402Transport(halvor, inner=my_transport)`. If you already have a client object, `wrap_httpx(client, halvor)` and `wrap_httpx_async(client, halvor)` observe every method except `stream()`.

Each 402 challenge becomes a `payment.requested` event. The payment that answers it becomes `payment.authorized`, linked to the challenge with `related.previous_event_id`, and the settlement becomes `payment.settled` or `payment.failed` with the same `payment_id`. A challenge without a network or token address is reported as `invalid_event` and not recorded.

## Stripe

Pass each verified Stripe webhook event to `halvor.stripe(event)`. PaymentIntents, refunds and disputes are recorded on the `stripe` rail. Stripe Issuing authorizations and transactions are recorded on the `card` rail. Any other event type returns `None` and is ignored. The adapter does not import the Stripe library, and it accepts a plain dict or a `stripe.Event`.

```python
import stripe

from halvorai_ingest import agent_context
from halvorai_ingest.stripe import tag_issuing_card, tag_payment_intent

# Tag what the agent creates, so its webhooks map back to the agent, owner and instruction.
with agent_context(agent={"id": "agt_travel_12", "name": "Travel agent"}, owner={"id": "own_4410", "type": "business"}):
    card = stripe.issuing.Card.create(**tag_issuing_card({"cardholder": "ich_...", "currency": "usd", "type": "virtual"}))
    intent = stripe.PaymentIntent.create(**tag_payment_intent({"amount": 41200, "currency": "usd"}))

# In your webhook endpoint, after Stripe's own signature check:
event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
halvor.stripe(event)
```

The tags are `metadata.halvor_*` keys, taken from `agent_context` or from keyword overrides (`agent_id`, `agent_name`, `framework`, `owner_id`, `owner_type`, `instruction_id`). Put them on the card for Issuing: card webhooks carry no other agent name. An event without tags is recorded as `unattributed`. `map_stripe_event(event)` returns the event without queuing it. The mapping is the same as in the Node.js and Go SDKs.

MPP and MCP adapters are in the Node.js SDK, [`@halvorai/ingest`](https://github.com/halvorAI/ingest-js). Guides for each: [docs.halvorai.com](https://docs.halvorai.com).

## Mobile apps (Flutter)

Apps built with [`halvorai_ingest_flutter`](https://github.com/halvorAI/ingest-dart) sign events on the phone, with a key that each install makes for itself. The app never holds your API key. Your backend registers each install's public key and forwards its signed batches; `halvorai_ingest.device` provides both. With FastAPI:

```python
from fastapi import FastAPI, Request, Response

from halvorai_ingest.device import (
    DeviceRequestError,
    forward_envelopes_async,
    register_device_source_async,
)

app = FastAPI()
UNAUTHORIZED = b'{"error":{"code":"unauthorized","message":"sign in first"}}'


@app.post("/halvor/sources")  # the app's provisionUrl
async def halvor_sources(request: Request) -> Response:
    user = await current_user(request)  # your own session check
    if user is None:
        return Response(UNAUTHORIZED, status_code=401, media_type="application/json")
    body = await request.json()
    try:
        source_id = await register_device_source_async(body.get("public_jwk"), name=body.get("name"))
    except DeviceRequestError as exc:
        return Response(exc.body(), status_code=exc.status, media_type="application/json")
    return Response(f'{{"source_id":"{source_id}"}}', media_type="application/json")


@app.post("/halvor/events")  # the app's forwardUrl
async def halvor_events(request: Request) -> Response:
    user = await current_user(request)
    if user is None:
        return Response(UNAUTHORIZED, status_code=401, media_type="application/json")
    out = await forward_envelopes_async(await request.body(), expected_owner_id=user.id)
    return Response(out.body, status_code=out.status, media_type=out.content_type)
```

Both functions read `HALVOR_API_KEY` (and `HALVOR_ENDPOINT`) from the environment, or take `api_key=`. Sync versions without `_async` work the same way in Flask or Django.

What they do:

- **`register_device_source`** accepts only the public half of an Ed25519 key, refusing a key with `d`. It registers the key with `POST /v1/sources` and returns the `src_` id.
- **`forward_envelopes`**:
  - Checks that every event's `owner.id` is the signed-in user and its `environment` is the key's.
  - Sends the request body to `POST /v1/events` **byte for byte**, never a parsed and re-serialized copy, so the device's signatures stay valid.
  - Returns HalvorAI's status and body unchanged, which the app reads its per-event results from.
- **Status codes tell the app what to do:**
  - 401 or 403 means "try later": the app keeps the batch on disk and retries.
  - 400, 413 and 422 are permanent: the app drops the batch, or splits it on 413.
  - 502 means HalvorAI couldn't be reached, and the app retries.

Set the signed-in user as `owner` in the app's agent context; events without it are refused.

## Develop

```bash
uv sync --extra dev
uv run pytest
uv run mypy
```

## Security

Report vulnerabilities privately to hello@halvorai.com.

## License

Apache-2.0
