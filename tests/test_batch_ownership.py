"""A farmer may act only on their own batches (P3-G follow-up).

Role checks answer "may this kind of actor do this kind of thing". They do not
answer "may *this* actor do it to *this* row", and the second question is the
one IDOR exploits. `04` P3 records the v1 instance: `POST /farmers/farm-details/
{id}` had JWT and role checks and no ownership check, so any farmer's token
could mutate any farmer's record. It is still unpatched in the deployed v1.

`batch.create` and `batch.harvest_record` both admit `Role.farmer`, so both
need the second check. Staff roles do not: an operator or admin acting on any
batch is the single-dashboard walk working as designed (`04` §5.3), and
attribution stays honest because every action names its actor in `audit_log`.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import BatchState, Role
from app.models import AuditLog, HarvestRecord, HoneyBatch
from tests.helpers import METADATA, auth, seed_apiary, seed_farmer, seed_user
from tests.test_transitions_endpoints import HARVEST


def test_a_farmer_may_not_create_a_batch_for_another_farmer(client, migrated_engine):
    mine = seed_farmer(migrated_engine, "+254700050001")
    theirs = seed_farmer(migrated_engine, "+254700050002")
    their_apiary = seed_apiary(migrated_engine, theirs)
    me = seed_user(migrated_engine, Role.farmer, "farmer-own-1", farmer_id=mine)

    r = client.post(
        "/v2/batches",
        json={"farmer_id": theirs, "apiary_id": their_apiary, "metadata": METADATA},
        headers=auth(me, Role.farmer),
    )

    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"
    with Session(migrated_engine) as s:
        assert s.execute(select(func.count()).select_from(HoneyBatch)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 0


def test_a_farmer_may_create_their_own_batch(client, migrated_engine):
    mine = seed_farmer(migrated_engine, "+254700050003")
    my_apiary = seed_apiary(migrated_engine, mine)
    me = seed_user(migrated_engine, Role.farmer, "farmer-own-2", farmer_id=mine)

    r = client.post(
        "/v2/batches",
        json={"farmer_id": mine, "apiary_id": my_apiary, "metadata": METADATA},
        headers=auth(me, Role.farmer),
    )

    assert r.status_code == 201, r.text


def test_a_farmer_may_not_record_a_harvest_on_another_farmers_batch(client, migrated_engine):
    """The transition IDOR: role alone would let any farmer harvest any batch."""
    theirs = seed_farmer(migrated_engine, "+254700050004")
    their_apiary = seed_apiary(migrated_engine, theirs)
    operator = seed_user(migrated_engine, Role.operator, "op-own")
    created = client.post(
        "/v2/batches",
        json={"farmer_id": theirs, "apiary_id": their_apiary, "metadata": METADATA},
        headers=auth(operator, Role.operator),
    )
    batch_id = created.json()["id"]

    mine = seed_farmer(migrated_engine, "+254700050005")
    me = seed_user(migrated_engine, Role.farmer, "farmer-own-3", farmer_id=mine)
    before = _audit_count(migrated_engine)

    r = client.post(f"/v2/batches/{batch_id}/harvest", json=HARVEST, headers=auth(me, Role.farmer))

    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"
    with Session(migrated_engine) as s:
        assert s.get(HoneyBatch, batch_id).state is BatchState.CREATED
        assert s.execute(select(func.count()).select_from(HarvestRecord)).scalar_one() == 0
    assert _audit_count(migrated_engine) == before


def test_a_farmer_may_record_a_harvest_on_their_own_batch(client, migrated_engine):
    mine = seed_farmer(migrated_engine, "+254700050006")
    my_apiary = seed_apiary(migrated_engine, mine)
    me = seed_user(migrated_engine, Role.farmer, "farmer-own-4", farmer_id=mine)
    headers = auth(me, Role.farmer)

    created = client.post(
        "/v2/batches",
        json={"farmer_id": mine, "apiary_id": my_apiary, "metadata": METADATA},
        headers=headers,
    )
    r = client.post(f"/v2/batches/{created.json()['id']}/harvest", json=HARVEST, headers=headers)

    assert r.status_code == 201, r.text


def test_a_farmer_credential_with_no_profile_may_not_act(client, migrated_engine):
    """A `farmer`-role user with no `farmers` row owns nothing, so it must be
    refused rather than matching a NULL against a batch's owner."""
    theirs = seed_farmer(migrated_engine, "+254700050007")
    their_apiary = seed_apiary(migrated_engine, theirs)
    orphan = seed_user(migrated_engine, Role.farmer, "farmer-orphan")

    r = client.post(
        "/v2/batches",
        json={"farmer_id": theirs, "apiary_id": their_apiary, "metadata": METADATA},
        headers=auth(orphan, Role.farmer),
    )

    assert r.status_code == 403


def test_an_operator_may_act_on_any_batch(client, migrated_engine):
    """Staff scope is deliberate: 04 §5.3 lets one actor walk a batch end to
    end, and audit_log.actor_id keeps the record of who actually did it."""
    farmer_id = seed_farmer(migrated_engine, "+254700050008")
    apiary_id = seed_apiary(migrated_engine, farmer_id)
    operator = seed_user(migrated_engine, Role.operator, "op-any")
    headers = auth(operator, Role.operator)

    created = client.post(
        "/v2/batches",
        json={"farmer_id": farmer_id, "apiary_id": apiary_id, "metadata": METADATA},
        headers=headers,
    )
    r = client.post(f"/v2/batches/{created.json()['id']}/harvest", json=HARVEST, headers=headers)

    assert r.status_code == 201, r.text
    with Session(migrated_engine) as s:
        row = s.execute(
            select(AuditLog).where(AuditLog.action == "batch.harvest_recorded")
        ).scalar_one()
        assert row.actor_id == operator
        assert row.actor_role == Role.operator


def _audit_count(engine) -> int:
    with Session(engine) as s:
        return s.execute(select(func.count()).select_from(AuditLog)).scalar_one()
