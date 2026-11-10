"""Write `metadata.halvor_*` on Stripe objects so their webhooks map back to the agent."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from ..context import current_context


def _merge(
    existing: Mapping[str, Any] | None,
    *,
    agent_id: str | None,
    agent_name: str | None,
    framework: str | None,
    owner_id: str | None,
    owner_type: Literal["person", "business"] | None,
    instruction_id: str | None,
) -> dict[str, Any]:
    ctx: Mapping[str, Any] = current_context() or {}
    agent: Mapping[str, Any] = ctx.get("agent") or {}
    owner: Mapping[str, Any] = ctx.get("owner") or {}
    instruction: Mapping[str, Any] = ctx.get("instruction") or {}
    meta: dict[str, Any] = dict(existing or {})

    agent_id = agent_id if agent_id is not None else agent.get("id")
    agent_name = agent_name if agent_name is not None else agent.get("name")
    framework = framework if framework is not None else agent.get("framework")
    owner_id = owner_id if owner_id is not None else owner.get("id")
    owner_type = owner_type if owner_type is not None else owner.get("type")
    instruction_id = instruction_id if instruction_id is not None else instruction.get("id")

    if agent_id is not None:
        meta["halvor_agent_id"] = agent_id
    # Cut to the APE limits, which also keeps them inside Stripe's 500-character metadata values.
    if agent_name is not None:
        meta["halvor_agent_name"] = agent_name[:128]
    if framework is not None:
        meta["halvor_agent_framework"] = framework[:64]
    if owner_id is not None:
        meta["halvor_owner_id"] = owner_id
    if owner_type is not None:
        meta["halvor_owner_type"] = owner_type
    if instruction_id is not None:
        meta["halvor_instruction_id"] = instruction_id
    return meta


def tag_payment_intent(
    params: Mapping[str, Any],
    *,
    agent_id: str | None = None,
    agent_name: str | None = None,
    framework: str | None = None,
    owner_id: str | None = None,
    owner_type: Literal["person", "business"] | None = None,
    instruction_id: str | None = None,
) -> dict[str, Any]:
    """
    Return a copy of the PaymentIntent create params with `metadata.halvor_*` written from the
    current `agent_context`, or from the keyword overrides, which win over the context.

        stripe.PaymentIntent.create(**tag_payment_intent({"amount": 500, "currency": "usd"}))
    """
    out = dict(params)
    out["metadata"] = _merge(
        params.get("metadata"),
        agent_id=agent_id,
        agent_name=agent_name,
        framework=framework,
        owner_id=owner_id,
        owner_type=owner_type,
        instruction_id=instruction_id,
    )
    return out


def tag_issuing_card(
    params: Mapping[str, Any],
    *,
    agent_id: str | None = None,
    agent_name: str | None = None,
    framework: str | None = None,
    owner_id: str | None = None,
    owner_type: Literal["person", "business"] | None = None,
    instruction_id: str | None = None,
) -> dict[str, Any]:
    """
    The same tagging for Issuing card creates (one card per agent is recommended). The agent
    name and framework are stored on the card, since card webhooks carry no other agent name.

        stripe.issuing.Card.create(**tag_issuing_card({"currency": "usd", "type": "virtual", ...}))
    """
    return tag_payment_intent(
        params,
        agent_id=agent_id,
        agent_name=agent_name,
        framework=framework,
        owner_id=owner_id,
        owner_type=owner_type,
        instruction_id=instruction_id,
    )
