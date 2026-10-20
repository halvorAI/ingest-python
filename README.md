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

halvor.flush()
```

`emit` returns at once and never raises. Instruction text is hashed on your machine and never sent. Anyone with the public half of the key (the JWK without `d`) can check an envelope with `verify_event(envelope["event"], envelope["sig"], public_key)`.

## Send to HalvorAI (early access)

With a sandbox key (`hk_test_…`) and a source key registered in the dashboard:

```python
import os

from halvorai_ingest import HalvorClient

halvor = HalvorClient(
    api_key=os.environ["HALVOR_API_KEY"],
    source_key=os.environ["HALVOR_SOURCE_KEY"],  # the private JWK as JSON
)
```

Call `halvor.close()` before your process exits so queued events are sent.

## x402

Wrap the `httpx` client your agent pays with. Payment requirements, signed payment payloads and settlement responses become events:

```python
import httpx

from halvorai_ingest.x402 import wrap_httpx

client = wrap_httpx(httpx.Client(), halvor)
```

Stripe, MPP and MCP adapters are in the Node.js SDK, [`@halvorai/ingest`](https://github.com/halvorAI/ingest-js). Guides for each: [docs.halvorai.com](https://docs.halvorai.com).

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
