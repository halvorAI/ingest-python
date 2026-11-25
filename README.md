# halvorai-ingest

<p>
<a href="https://github.com/halvorAI/ingest-python/actions/workflows/ci.yml"><img height="16" alt="CI" src="https://github.com/halvorAI/ingest-python/actions/workflows/ci.yml/badge.svg"></a>
<a href="https://pypi.org/project/halvorai-ingest/"><img height="16" alt="PyPI" src="https://img.shields.io/pypi/v/halvorai-ingest?style=flat-square&label=pypi&color=4A6100"></a>
<a href="https://pypi.org/project/halvorai-ingest/"><img height="16" alt="Python versions" src="https://img.shields.io/pypi/pyversions/halvorai-ingest?style=flat-square&color=4A6100"></a>
<a href="https://github.com/halvorAI/ingest-python/blob/main/LICENSE"><img height="16" alt="License" src="https://img.shields.io/badge/license-Apache--2.0-4A6100?style=flat-square"></a>
<a href="https://github.com/halvorAI/ape-schema"><img height="16" alt="APE v0.2" src="https://img.shields.io/badge/APE-v0.2-C6F432?style=flat-square&labelColor=1C1C1C"></a>
</p>

Signed, attributed payment events for AI agents. Each event records which agent paid, for which owner, on which instruction, with the network proof that came with the payment.

It's for Python teams whose agents pay: agent wallets, agent cards, agentic checkout, and agents that buy APIs over x402.

<p align="center"><picture><source media="(min-width: 1012px)" srcset="https://raw.githubusercontent.com/halvorAI/ape-schema/main/assets/figures/hero-wide.svg"><img src="https://raw.githubusercontent.com/halvorAI/ape-schema/main/assets/figures/hero-narrow.svg" alt="An AI agent pays a merchant over a payment rail such as Stripe, a card, x402 or MPP. HalvorAI is not on that path. Beside it, the HalvorAI SDK observes the payment read-only and records a signed event, events are assembled into a signed evidence pack, and the bank verifies the pack offline."></picture></p>

HalvorAI records and proves agent payments after the fact. It is never in the payment path and cannot slow or block a payment.

> **Developer preview.** The SDK works offline today: build, sign and inspect events with your own sink. Sandbox API keys go to early-access teams. [Request access](https://halvorai.com/?utm_source=github&utm_campaign=ingest_python#early-access).

## Try it

Sign an event and check it, offline. You need [uv](https://docs.astral.sh/uv/); there is nothing to clone and no account:

```bash
uvx --from halvorai-ingest==0.6.0 halvorai-ingest demo
```

<p align="center"><img src="https://raw.githubusercontent.com/halvorAI/ingest-python/main/assets/demo.svg" width="860" alt="Output of the demo: an event is signed and its signature verified, then the same event with a changed amount fails verification."></p>

```text
1. Sign an event on this machine
   evt_…: Travel booking agent paid Example Airways 412.00 USD on stripe
   Instruction sent as sha256:3645911b116c…, never as text
   Ed25519 signature: verified

2. Change the amount from 41200 to 41290 and verify again
   Ed25519 signature: FAILED, the event was changed after signing
```

With pip: `pip install halvorai-ingest==0.6.0`, then `python -m halvorai_ingest demo`.

## Install

```bash
pip install halvorai-ingest
```

Python 3.10 or later. Event format: [Agent Payment Event (APE) v0.1](https://schema.halvorai.com/ape/v0.1.json) ([source](https://github.com/halvorAI/ape-schema)), open and Apache-2.0.

## Record events offline

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

Events are batched and sent in the background, retried with backoff, and can be kept on disk during an outage. Long-running processes need nothing else; short-lived scripts and serverless handlers call `halvor.flush(timeout=5)` before they return.

<details>
<summary><b>Batching, retries, spool and stats</b></summary>

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

</details>

## x402

Put the HalvorAI transport under the `httpx` client your agent pays with. Payment requirements, signed payment payloads and settlement responses become events. Headers are only read; nothing in the payment path changes.

```python
import httpx

from halvorai_ingest.x402 import AsyncHalvorX402Transport, HalvorX402Transport

client = httpx.Client(transport=HalvorX402Transport(halvor))
async_client = httpx.AsyncClient(transport=AsyncHalvorX402Transport(halvor))
```

<details>
<summary><b>Custom transports, and which events it records</b></summary>

To keep a custom transport, pass it as `inner`: `HalvorX402Transport(halvor, inner=my_transport)`. If you already have a client object, `wrap_httpx(client, halvor)` and `wrap_httpx_async(client, halvor)` observe every method except `stream()`.

Each 402 challenge becomes a `payment.requested` event. The payment that answers it becomes `payment.authorized`, linked to the challenge with `related.previous_event_id`, and the settlement becomes `payment.settled` or `payment.failed` with the same `payment_id`. A challenge without a network or token address is reported as `invalid_event` and not recorded.

</details>

## Stripe

Pass each verified Stripe webhook event to `halvor.stripe(event)`, and tag what the agent creates so webhooks map back to the agent.

<details>
<summary><b>Set up the Stripe adapter</b></summary>

PaymentIntents, refunds and disputes are recorded on the `stripe` rail. Stripe Issuing authorizations and transactions are recorded on the `card` rail. Any other event type returns `None` and is ignored. The adapter does not import the Stripe library, and it accepts a plain dict or a `stripe.Event`.

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

The tags are `metadata.halvor_*` keys, taken from `agent_context` or from keyword overrides (`agent_id`, `agent_name`, `framework`, `owner_id`, `owner_type`, `instruction_id`). Put them on the card for Issuing: card webhooks carry no other agent name. An event without tags is recorded as `unattributed`. `map_stripe_event(event)` returns the event without queuing it. The mapping is the same as in the Node.js SDK.

</details>

MPP and MCP adapters are in the Node.js SDK, [`@halvorai/ingest`](https://github.com/halvorAI/ingest-js). Guides for each: [docs.halvorai.com](https://docs.halvorai.com).

## Choose your next step

| I want to | Read |
| --- | --- |
| Send my first event to the sandbox | [Quickstart](https://docs.halvorai.com/get-started/quickstart/) |
| Look up an event field | [APE fields](https://github.com/halvorAI/ape-schema/blob/main/docs/FIELDS.md) |
| Record what the agent told its owner | [`emit_report`](https://docs.halvorai.com/sdks/python/#agent-reports) |
| Record MPP or MCP payments, or verify an evidence pack | [`@halvorai/ingest`](https://github.com/halvorAI/ingest-js) for Node.js |
| Read all the guides | [docs.halvorai.com](https://docs.halvorai.com) |

## Develop

```bash
uv sync --locked --extra dev
uv run mypy
uv run pytest -q
```

## Contributing

Bug fixes, tests, offline examples and doc fixes are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has the setup, the checks CI runs and how quickly we respond. Starter tasks are labeled [`good first issue`](https://github.com/halvorAI/ingest-python/labels/good%20first%20issue).

## Security

Report vulnerabilities privately to hello@halvorai.com, not in public issues. See [SECURITY.md](SECURITY.md).

## License

Apache-2.0
