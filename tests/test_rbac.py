"""RBAC dependency gating (P1-D). Builds a tiny app, overrides the current
user, and checks the @requires action table allows/blocks by role and returns
the standard error envelope."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.deps import ACTION_ROLES, get_current_user, requires
from app.errors import APIError, api_error_handler
from app.models import Role, User


def _app():
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)

    @app.get("/batch", dependencies=[Depends(requires("batch.create"))])
    def create_batch():
        return {"ok": True}

    return app


def _client_as(role: Role) -> TestClient:
    app = _app()
    app.dependency_overrides[get_current_user] = lambda: User(id=1, role=role, is_active=True)
    return TestClient(app)


def test_permitted_role_passes():
    res = _client_as(Role.operator).get("/batch")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_wrong_role_is_forbidden_with_envelope():
    res = _client_as(Role.lab_officer).get("/batch")
    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "forbidden"
    assert body["details"]["action"] == "batch.create"


def test_missing_token_is_unauthorized():
    # No override -> real get_current_user runs with no bearer credentials.
    app = _app()
    res = TestClient(app).get("/batch")
    assert res.status_code == 401
    assert res.json()["code"] == "unauthorized"


def test_unknown_action_fails_fast_at_definition():
    with pytest.raises(KeyError):
        requires("does.not.exist")


def test_action_table_is_the_single_source():
    # Guards that the two Phase 1 actions are declared; extend as actions land.
    assert "farmer.enroll" in ACTION_ROLES
    assert "batch.create" in ACTION_ROLES
