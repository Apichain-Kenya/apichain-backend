"""Shared request-schema constraints, applied by declaration (P3-D, 10 §3).

Two whole classes of intermittent failure are closed here by types rather than
by remembering:

**U+0000.** PostgreSQL `text` cannot store a NUL byte. Client text that reaches
a query with one in it comes back as a `DataError`, which `data_error_handler`
turns into a 400 — a status the endpoint's schema never promised. `08` closed
this for `LoginRequest.identifier` with an inline pattern; Phase 3a adds around
ten new free-text fields across six endpoints, which is too many to remember
one at a time.

**int32 overflow.** Every id column in this schema is a 32-bit integer. An id
past that bound reaches the driver as a numeric-overflow `DataError` rather
than being rejected as invalid input. Phase 2 bounded the *path* parameters;
these are the same values arriving in a request *body*, which were never
bounded. `BatchCreateRequest.farmer_id` has been unbounded since Phase 1, and
is a candidate explanation for the single unreproduced `POST /v2/batches`
contract failure the Phase 2 handoff carries forward as known-limitation 2.

The enumeration test is the point. Adding an eleventh free-text field without
the shared type fails here, at declaration, rather than once in fourteen
Schemathesis runs.
"""

import importlib
import pkgutil
from types import UnionType
from typing import Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

import app.schemas
from app.schemas.common import EntityId, SafeStr


class _Sample(BaseModel):
    text: SafeStr
    entity: EntityId


def test_a_nul_byte_is_rejected_as_invalid_input():
    with pytest.raises(ValidationError):
        _Sample(text="ab\x00cd", entity=1)


def test_ordinary_text_including_unicode_is_accepted():
    model = _Sample(text="Nyeri — mūratina 🍯", entity=1)

    assert model.text == "Nyeri — mūratina 🍯"


def test_an_id_beyond_int32_is_rejected_as_invalid_input():
    with pytest.raises(ValidationError):
        _Sample(text="x", entity=2_147_483_648)


def test_a_non_positive_id_is_rejected():
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            _Sample(text="x", entity=bad)


def test_the_largest_valid_int32_id_is_accepted():
    assert _Sample(text="x", entity=2_147_483_647).entity == 2_147_483_647


def _request_models() -> list[type[BaseModel]]:
    """Every Pydantic model in app.schemas whose name ends in `Request`."""
    models: list[type[BaseModel]] = []
    for info in pkgutil.iter_modules(app.schemas.__path__):
        module = importlib.import_module(f"app.schemas.{info.name}")
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, BaseModel) and name.endswith("Request"):
                models.append(obj)
    return models


def _leaf_types(annotation: object) -> list[object]:
    if get_origin(annotation) in (Union, UnionType):
        return [a for a in get_args(annotation) if a is not type(None)]
    return [annotation]


def test_there_are_request_models_to_check():
    """Guard the guard: a broken discovery helper would make the next two
    tests pass vacuously."""
    assert len(_request_models()) >= 4


def test_every_free_text_request_field_forbids_a_nul_byte():
    offenders = [
        f"{model.__name__}.{name}"
        for model in _request_models()
        for name, field in model.model_fields.items()
        if str in _leaf_types(field.annotation)
        and not any(getattr(m, "pattern", None) for m in field.metadata)
    ]

    assert offenders == [], f"use SafeStr for these: {offenders}"


def test_every_integer_request_field_is_bounded_to_int32():
    offenders = [
        f"{model.__name__}.{name}"
        for model in _request_models()
        for name, field in model.model_fields.items()
        if int in _leaf_types(field.annotation)
        and bool not in _leaf_types(field.annotation)
        and not any(getattr(m, "le", None) for m in field.metadata)
    ]

    assert offenders == [], f"bound these to int32: {offenders}"
