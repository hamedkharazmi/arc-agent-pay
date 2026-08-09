from __future__ import annotations

from pydantic import BaseModel, Field

from arc_agent_pay._payment_identifier import (
    PAYMENT_IDENTIFIER,
    append_payment_identifier_to_extensions,
    generate_payment_id,
    is_payment_identifier_extension,
    is_valid_payment_id,
)


class _Info(BaseModel):
    required: bool


class _Extension(BaseModel):
    info: _Info
    schema_: dict = Field(alias="schema")


def test_generated_payment_id_matches_x402_wire_constraints():
    payment_id = generate_payment_id()

    assert payment_id.startswith("pay_")
    assert is_valid_payment_id(payment_id)


def test_payment_id_validation_enforces_length_and_character_set():
    assert is_valid_payment_id("payment_order_0001")
    assert not is_valid_payment_id("too-short")
    assert not is_valid_payment_id("payment order 0001")
    assert not is_valid_payment_id("x" * 129)
    assert not is_valid_payment_id(None)


def test_append_preserves_declared_extension_and_adds_id():
    declaration = {
        "info": {"required": False, "seller-field": "preserved"},
        "schema": {"type": "object"},
        "seller-extension": True,
    }
    extensions = {PAYMENT_IDENTIFIER: declaration, "another-extension": {}}

    returned = append_payment_identifier_to_extensions(
        extensions, "payment_order_0001"
    )

    assert returned is extensions
    assert extensions[PAYMENT_IDENTIFIER]["info"]["id"] == "payment_order_0001"
    assert extensions[PAYMENT_IDENTIFIER]["info"]["seller-field"] == "preserved"
    assert extensions[PAYMENT_IDENTIFIER]["schema"] == {"type": "object"}
    assert extensions[PAYMENT_IDENTIFIER]["seller-extension"] is True
    assert extensions["another-extension"] == {}
    assert "id" not in declaration["info"]


def test_append_accepts_pydantic_declaration_without_optional_x402_extras():
    extensions = {
        PAYMENT_IDENTIFIER: _Extension.model_validate(
            {"info": {"required": True}, "schema": {"type": "object"}}
        )
    }

    append_payment_identifier_to_extensions(extensions, "payment_order_0002")

    assert extensions[PAYMENT_IDENTIFIER]["info"] == {
        "required": True,
        "id": "payment_order_0002",
    }
    assert extensions[PAYMENT_IDENTIFIER]["schema"] == {"type": "object"}


def test_invalid_or_undeclared_extension_is_left_unchanged():
    extensions = {PAYMENT_IDENTIFIER: {"info": {"required": "yes"}}}

    returned = append_payment_identifier_to_extensions(extensions, "invalid id")

    assert returned is extensions
    assert extensions == {PAYMENT_IDENTIFIER: {"info": {"required": "yes"}}}
    assert not is_payment_identifier_extension(extensions[PAYMENT_IDENTIFIER])
