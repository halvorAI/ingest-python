"""APE v0.1 validation before signing, so a malformed event is dropped and reported, never sent.

Mirrors the TypeScript SDK's zod model (`packages/schema/src/ape.ts`), which matches the published
JSON Schema at https://schema.halvorai.com/ape/v0.1.json, plus the card-number guard.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)


def _rfc3339(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("must be an RFC 3339 date-time with an offset")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _url(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+", value):
        raise ValueError("must be a URL")
    return value


def _max(n: int) -> Any:
    return StringConstraints(strict=True, max_length=n)


S16 = Annotated[str, _max(16)]
S32 = Annotated[str, _max(32)]
S64 = Annotated[str, _max(64)]
S128 = Annotated[str, _max(128)]
S256 = Annotated[str, _max(256)]
S500 = Annotated[str, _max(500)]
EventId = Annotated[str, StringConstraints(strict=True, min_length=8, max_length=128)]
DateTime = Annotated[str, StringConstraints(strict=True), AfterValidator(_rfc3339)]
Url = Annotated[str, StringConstraints(strict=True), AfterValidator(_url)]
Country = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z]{2}$")]
Caip2 = Annotated[str, StringConstraints(strict=True, pattern=r"^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}$")]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
AmountValue = Annotated[str, StringConstraints(strict=True, pattern=r"^(0|[1-9][0-9]*)$")]
Currency = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z]{3}$")]
FourDigits = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]{4}$")]
RefValue = Union[Annotated[str, StringConstraints(strict=True)], Annotated[int, Field(strict=True)]]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _no_nulls(cls, data: Any) -> Any:
        # The JSON Schema has no nullable fields: an optional field is left out, never null.
        if isinstance(data, Mapping):
            nulls = [k for k, v in data.items() if v is None]
            if nulls:
                raise ValueError(f"null is not allowed for {', '.join(map(str, nulls))}; leave the field out")
        return data



class Source(_Strict):
    id: S64
    sdk: S64
    adapter: Literal[
        "stripe", "stripe-issuing", "x402-client", "x402-server", "mpp-server", "mcp", "manual", "custom"
    ]


class Kya(_Strict):
    provider: S64
    subject: S256
    assurance: S64 | None = None
    verified_at: DateTime | None = None


class Agent(_Strict):
    id: S128
    name: S128 | None = None
    framework: S64 | None = None
    version: S64 | None = None
    key_thumbprint: S64 | None = None
    kya: Kya | None = None


class Owner(_Strict):
    id: S128
    type: Literal["person", "business"]
    display_name: S128 | None = None
    country: Country | None = None
    kyc_ref: S128 | None = None


class Instruction(_Strict):
    id: S128
    hash: Sha256
    summary: S500 | None = None
    channel: Literal["chat", "api", "schedule", "email", "agent", "other"] | None = None
    issued_at: DateTime | None = None
    tool_call_id: S128 | None = None


class Asset(_Strict):
    network: Caip2
    address: S128
    symbol: S16 | None = None
    decimals: Annotated[int, Field(strict=True, ge=0, le=36)] | None = None


class MinorAmount(_Strict):
    value: AmountValue
    unit: Literal["minor"]
    currency: Currency
    asset: Asset | None = None


class AtomicAmount(_Strict):
    value: AmountValue
    unit: Literal["atomic"]
    asset: Asset
    currency: Currency | None = None


Amount = Annotated[Union[MinorAmount, AtomicAmount], Field(discriminator="unit")]
PaymentStatus = Literal["requested", "authorized", "settled", "failed", "refunded", "disputed"]


class _PaymentBase(_Strict):
    payment_id: S256
    amount: Amount
    status: PaymentStatus
    failure_reason: S128 | None = None
    description: S256 | None = None
    category: S64 | None = None

    @model_validator(mode="after")
    def _failure_reason(self) -> "_PaymentBase":
        if self.status == "failed" and self.failure_reason is None:
            raise ValueError("failure_reason is required when status is failed")
        return self


class StripeRef(_Strict):
    account: RefValue | None = None
    payment_intent: RefValue | None = None
    charge: RefValue | None = None
    refund: RefValue | None = None
    dispute: RefValue | None = None
    shared_payment_token: RefValue | None = None


class CardRef(_Strict):
    authorization_id: RefValue | None = None
    transaction_id: RefValue | None = None
    network_transaction_id: RefValue | None = None
    processor: RefValue | None = None


class X402Ref(_Strict):
    scheme: RefValue | None = None
    network: RefValue | None = None
    nonce: RefValue | None = None
    tx_hash: RefValue | None = None
    facilitator: RefValue | None = None
    payment_flow: RefValue | None = None


class MppRef(_Strict):
    method: RefValue | None = None
    challenge_id: RefValue | None = None
    payment_intent: RefValue | None = None
    tx_hash: RefValue | None = None


class StablecoinRef(_Strict):
    network: RefValue | None = None
    tx_hash: RefValue | None = None
    log_index: RefValue | None = None


class BankTransferRef(_Strict):
    scheme: RefValue | None = None
    end_to_end_id: RefValue | None = None
    pisp: RefValue | None = None


class StripePayment(_PaymentBase):
    rail: Literal["stripe"]
    rail_ref: StripeRef


class CardPayment(_PaymentBase):
    rail: Literal["card"]
    rail_ref: CardRef


class X402Payment(_PaymentBase):
    rail: Literal["x402"]
    rail_ref: X402Ref


class MppPayment(_PaymentBase):
    rail: Literal["mpp"]
    rail_ref: MppRef


class StablecoinPayment(_PaymentBase):
    rail: Literal["stablecoin"]
    rail_ref: StablecoinRef


class BankTransferPayment(_PaymentBase):
    rail: Literal["bank_transfer"]
    rail_ref: BankTransferRef


Payment = Annotated[
    Union[StripePayment, CardPayment, X402Payment, MppPayment, StablecoinPayment, BankTransferPayment],
    Field(discriminator="rail"),
]


class Wallet(_Strict):
    network: Caip2
    address: S128


class Payee(_Strict):
    type: Literal["merchant", "api_provider", "wallet", "business", "person"]
    name: S256 | None = None
    id: S128 | None = None
    website: Url | None = None
    country: Country | None = None
    mcc: FourDigits | None = None
    wallet: Wallet | None = None
    resource_url: Url | None = None

    @model_validator(mode="after")
    def _identified(self) -> "Payee":
        if self.name is None and self.wallet is None and self.id is None:
            raise ValueError("payee needs at least one of name, wallet or id")
        return self


class Instrument(_Strict):
    type: Literal["card", "agentic_token", "stripe_spt", "wallet", "bank_account"]
    network: S32 | None = None
    last4: FourDigits | None = None
    par: S64 | None = None
    token_ref: S128 | None = None
    wallet: Wallet | None = None
    issuer: S128 | None = None


class Evidence(_Strict):
    kind: Literal[
        "ap2.payment_mandate",
        "ap2.checkout_mandate",
        "vi.chain",
        "tap.signature",
        "x402.payment_payload",
        "x402.settlement",
        "mpp.receipt",
        "stripe.object",
        "agentic_token.metadata",
        "kya.attestation",
    ]
    format: Literal["sd-jwt", "kb-sd-jwt", "jws", "http-signature", "json"]
    value: Union[Annotated[str, StringConstraints(strict=True)], dict[str, Any]] | None = None
    uri: Url | None = None
    sha256: Sha256
    captured_at: DateTime | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "Evidence":
        if (self.value is None) == (self.uri is None):
            raise ValueError("evidence needs exactly one of value or uri")
        return self


class Related(_Strict):
    previous_event_id: S128 | None = None
    corrects_event_id: S128 | None = None
    session_id: S128 | None = None


Labels = Annotated[dict[S64, S256], Field(max_length=20)]


def contains_pan(text: str) -> bool:
    """True when text holds a Luhn-valid run of 13 to 19 digits (spaces or dashes allowed)."""
    # Same boundaries as the redaction: digits glued to letters (hex, base64, ids) are not a card.
    for run in re.findall(r"(?<![0-9A-Za-z])\d(?:[ -]?\d)*(?![0-9A-Za-z])", text):
        digits = re.sub(r"[ -]", "", run)
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class ApeEvent(_Strict):
    ape_version: Literal["0.1"]
    id: EventId
    type: Literal[
        "payment.requested",
        "payment.authorized",
        "payment.settled",
        "payment.failed",
        "payment.refunded",
        "payment.disputed",
    ]
    occurred_at: DateTime
    environment: Literal["test", "live"]
    source: Source
    agent: Agent
    owner: Owner
    instruction: Instruction | None = None
    payment: Payment
    payee: Payee
    instrument: Instrument | None = None
    evidence: Annotated[list[Evidence], Field(max_length=16)] | None = None
    related: Related | None = None
    labels: Labels | None = None

    @model_validator(mode="after")
    def _no_card_numbers(self) -> "ApeEvent":
        texts = [
            self.payment.description,
            self.payment.category,
            self.payment.failure_reason,
            self.instruction.summary if self.instruction else None,
            self.agent.name,
            self.owner.display_name,
            self.payee.name,
            self.payee.website,
            self.payee.resource_url,
            *(self.labels or {}).keys(),
            *(self.labels or {}).values(),
        ]
        if any(t is not None and contains_pan(t) for t in texts):
            raise ValueError("pan_detected")
        return self


_EVENT = TypeAdapter(ApeEvent)


def validate_event(event: Mapping[str, Any]) -> list[str]:
    """Problems with the event, as `path: message` strings. Empty when the event is valid."""
    try:
        _EVENT.validate_python(dict(event))
        return []
    except ValidationError as exc:
        return [".".join(str(p) for p in err["loc"]) + ": " + err["msg"] for err in exc.errors()]
