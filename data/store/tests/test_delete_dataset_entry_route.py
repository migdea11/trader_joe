"""DELETE /store/{id}: the authorisation decision, the two failure codes, and the one property that is load-bearing.

WHY THIS FILE EXISTS (validator, gating tj-v0r2pk and tj-uupb4q item 2). Commit 6e0c43f made
``DELETE /store/{id}`` pass the declared owner through to ``delete_entry_by_id`` and mapped
``OwnerMismatch`` to 403 and ``EntryNotFound`` to 404. Before it, every authenticated DELETE was a
TypeError against a three-parameter function and both exceptions were unhandled ValueErrors -- so
the mappings existed NOWHERE in the tree. The only coverage this route had was the malformed-path
422 case in test_http_smoke.py, which never reaches the handler: a destructive endpoint that as of
today also carries an authorisation decision had no test that deleted anything or observed a
refusal.

WHAT TIER THIS IS, and what it therefore cannot say. The route is driven through TestClient against
a recording fake session. There is no Postgres, so a status code observed here is the one FastAPI
assembled and not one a live deployment returned, and three claims are NOT proved here and must not
be inferred from this file being green:
  * that ON DELETE CASCADE removes exactly this entry's bars -- host-verified tier, tj-vhboky.14;
    the model-side half (the FK declares the clause) is pinned in test_dataset_entry_identity.py
  * that the DELETE statement matches a real row at all
  * anything about concurrency between the SELECT and the DELETE
What IS proved is the half a unit-tier test owns: which code the route answers for each of the four
cases tj-v0r2pk names, that no SQL is sent on a refusal, and that the refusal cannot be turned into
an authorisation bypass by a plausible tidy-up.

>>> THE ONE PROPERTY THAT IS LOAD-BEARING: A None OWNER MUST NEVER AUTHORISE. <<<

``AssetDatasetStoreDelete.owner`` is deliberately ``str | None = None`` so that an owner-less delete
reaches the handler and is REFUSED there rather than reported as a validation error -- an absent
owner is the degenerate case of a wrong one, and splitting one authorisation failure across 403 and
422 would be the API lying about what went wrong. That is only safe because of an incidental fact:
``_check_owner`` compares ``existing.owner != declared_owner``, and ``StoreDatasetEntry.owner`` is
``nullable=False`` with ``server_default='unassigned'``, so the stored value is never None, the
comparison is always true for a None declaration, and None cannot authorise.

Two plausible tidy-ups turn that into an authorisation bypass, and NEITHER changes a line that looks
like security code:
  1. ``if declared_owner and existing.owner != declared_owner`` in ``_check_owner`` -- the
     "don't compare against None" refactor. An owner-less caller may then delete anybody's data.
  2. ``delete_entry_by_id(db, request_path.id, request_path.owner or 'unassigned')`` in the router --
     the "don't pass None downstream" refactor. An owner-less caller may then delete every entry
     still carrying the migration's server default, which is every entry created before owners
     existed.
Mutation 2 is why the owner-less route case below is PARAMETRIZED OVER THE STORED OWNER, 'unassigned'
included: a single case using a normal owner stays green under it, and would have been decoration.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from data.store.app.database.crud.stock import store_dataset_entry as crud
from data.store.app.database.database import async_db
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.main import app
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER


pytestmark = pytest.mark.data_store

# The principal that owns the entry in the fixtures below, and the one the route declares when it is
# meant to succeed.
OWNER = 'strategy-a'

# The migration's server default (eec8f88a7443). Every entry that predates owner-scoped writes
# carries it, which is what makes it the value a bypass would unlock -- see mutation 2 in the header.
UNASSIGNED = 'unassigned'

CONFIGURED_SECRET = 'delete-route-instance-secret'

# The route's name, which FastAPI takes from the endpoint's __name__ and which app.url_path_for()
# resolves through the prefix data/store/app/main.py mounts the router under. It is also the symbol
# routers/tests/interface_manifest/data_store.manifest records for this address.
ROUTE_NAME = 'delete_data'


class FakeResult:
    """One canned answer, faithful to the two accessors the delete path uses.

    ``rowcount`` is a real int because ``delete_entry_by_id`` compares it to 0 -- a MagicMock
    compares unequal to 0 forever, which is the exact shape of the dead ``if result == 0`` check
    Amendment 1 item R was raised to fix.
    """

    def __init__(self, entry: StoreDatasetEntry | None = None, rowcount: int = 1):
        self._entry = entry
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> StoreDatasetEntry | None:
        return self._entry


class FakeSession:
    """An async session that records every statement and reaches no database.

    The recording is the point: "it answered 403" is not the whole claim. A refusal that answered
    403 AFTER sending the DELETE would satisfy the status assertion and would have deleted the row,
    so every refusal case below also asserts that no DELETE was ever sent.
    """

    def __init__(self, *results: FakeResult):
        self._results = list(results)
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        assert self._results, 'the delete path issued more statements than the fixture planned for'
        return self._results.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        return None


def stored_entry(entry_id: uuid.UUID, owner: str = OWNER) -> StoreDatasetEntry:
    """A detached ORM instance standing in for a row already in the table.

    Only ``id`` and ``owner`` are set: they are the two attributes ``_check_owner`` and the DELETE
    statement read. Inventing values for the other eight identity columns would suggest this file
    asserts something about them, and it does not.

    Args:
        entry_id (uuid.UUID): The entry's id.
        owner (str): The principal the row belongs to.

    Returns:
        StoreDatasetEntry: The stand-in row.
    """
    return StoreDatasetEntry(id=entry_id, owner=owner)


def _sql(statement) -> str:
    """The statement as Postgres would receive it, whitespace-normalised."""
    return ' '.join(str(statement.compile(dialect=postgresql.dialect())).split())


def _sent_delete(session: FakeSession) -> list:
    """Every DELETE statement the session was handed.

    Returns:
        list: The DELETE statements, which every refusal case requires to be empty.
    """
    return [statement for statement in session.statements if _sql(statement).startswith('DELETE')]


@pytest.fixture
def delete_entry(monkeypatch: pytest.MonkeyPatch):
    """A callable that drives the real DELETE route against a given fake session.

    The instance secret is configured and sent, because the guard is a decorator-level dependency and
    is therefore answered BEFORE any of this route's own parameters are read -- an unauthenticated
    request never reaches the handler and could not exercise a single assertion in this file. The 401
    itself, and that ordering, are pinned in test_http_smoke.py; they are not this file's subject.

    The URL is asked of ``app.url_path_for`` rather than written out, for the reason
    test_http_smoke.py gives at length: only data/store/app/main.py knows what prefix the router is
    mounted under, and hard-coding '/store/{id}' would assume the answer.

    ``raise_server_exceptions`` is left at its default True: an unhandled exception in the handler --
    the TypeError this route used to raise on every call, say -- arrives as its own traceback rather
    than as an opaque 500.

    Args:
        monkeypatch: Sets INSTANCE_WRITE_SECRET for the duration of one test.

    Yields:
        Callable: (session, entry_id, owner) -> httpx.Response. ``owner`` omitted sends no owner
            query parameter at all, which is the owner-less request.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)
    _absent = object()

    def send(session: FakeSession, entry_id: uuid.UUID, owner=_absent):
        app.dependency_overrides[async_db] = lambda: session
        try:
            client = TestClient(app)
            return client.delete(
                app.url_path_for(ROUTE_NAME, id=str(entry_id)),
                params={} if owner is _absent else {'owner': owner},
                headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET},
            )
        finally:
            # `app` is a module-level singleton other test modules import.
            app.dependency_overrides.clear()

    yield send


# ---------------------------------------------------------------------------------------------
# The four cases tj-v0r2pk names
# ---------------------------------------------------------------------------------------------


def test_the_owner_of_an_entry_deletes_it(delete_entry):
    """The happy path, which no test reached before this file: an owner deletes their own entry.

    The bars go with it by ON DELETE CASCADE, which is why there is exactly ONE statement and it does
    not name the bar table -- the sweep that used to build a ~65,000-element IN-list is gone (D1).
    THAT THE CASCADE ACTUALLY REMOVES THEM IS NOT PROVED HERE: no Postgres is reachable, and the
    model-side half lives in test_dataset_entry_identity.py. What is proved is that the route commits
    one delete against the entry table and nothing else, so nothing but the cascade could remove a
    bar.
    """
    entry_id = uuid.uuid4()
    session = FakeSession(FakeResult(stored_entry(entry_id)), FakeResult(rowcount=1))

    response = delete_entry(session, entry_id, OWNER)

    assert response.status_code == 200, response.text
    assert response.json() == {'message': 'Data deleted'}
    sent = _sent_delete(session)
    assert len(sent) == 1, f'the route sent {len(sent)} DELETE statements'
    sql = _sql(sent[0])
    assert sql.startswith(f'DELETE FROM {StoreDatasetEntry.TABLE_NAME}')
    assert StockMarketActivity.TABLE_NAME not in sql, f'the route still sweeps the bar table: {sql}'
    assert session.commits == 1, 'the delete was never committed'


def test_a_wrong_owner_is_refused_with_403_and_nothing_is_deleted(delete_entry):
    """OwnerMismatch -> 403, mapped nowhere in the tree before 6e0c43f.

    The status is half the claim. ``no DELETE was sent`` is the other half: a check that ran after the
    statement would answer the same 403 and would already have deleted the row, and that is the
    defect the check exists to prevent rather than a stylistic preference.
    """
    entry_id = uuid.uuid4()
    session = FakeSession(FakeResult(stored_entry(entry_id, owner=OWNER)))

    response = delete_entry(session, entry_id, 'someone-else')

    assert response.status_code == 403, response.text
    assert _sent_delete(session) == [], 'the entry was deleted despite the owner mismatch'
    assert session.commits == 0


@pytest.mark.parametrize('stored_owner', [OWNER, UNASSIGNED], ids=['a-normal-owner', 'the-migration-default'])
def test_an_owner_less_request_is_refused_with_403(delete_entry, stored_owner: str):
    """THE CASE THAT MATTERS MOST, and the reason it is parametrized. Read the module header first.

    An absent owner authorises nothing today only because the stored value is never None, so the
    comparison is always true. That is correct and INCIDENTAL, and two refactors that look like
    tidy-ups turn it into a bypass. This case is what makes either one red.

    'unassigned' IS THE SECOND PARAMETER AND NOT DECORATION. Under the router-side mutation
    ``request_path.owner or 'unassigned'`` a single case using a normal owner stays green -- the
    substituted default still mismatches 'strategy-a' -- while every entry created before owners
    existed becomes deletable by an unauthenticated-as-anyone caller. Measured, not assumed: that
    mutation reds this test only on the-migration-default parameter.

    403 AND NOT 422 is the ruling, not an accident of the schema (tj-v0r2pk): owner is an
    authorisation assertion rather than a data field, so a missing one is a refusal to authorise and
    not a malformed request. Making the field required would report an authorisation failure as a
    validation error and split one failure across two status codes.

    Args:
        delete_entry: Drives the real DELETE route against a fake session.
        stored_owner: The owner on the row the request tries to delete.
    """
    entry_id = uuid.uuid4()
    session = FakeSession(FakeResult(stored_entry(entry_id, owner=stored_owner)))

    response = delete_entry(session, entry_id)

    assert response.status_code == 403, (
        f'an owner-less DELETE against an entry owned by {stored_owner!r} answered '
        f'{response.status_code}: {response.text}. A None owner must never authorise.'
    )
    assert _sent_delete(session) == [], 'an owner-less DELETE reached the delete statement'
    assert session.commits == 0


def test_an_unknown_id_is_404(delete_entry):
    """EntryNotFound -> 404. 'No such entry' and 'not yours' are different answers."""
    entry_id = uuid.uuid4()
    session = FakeSession(FakeResult(None))

    response = delete_entry(session, entry_id, OWNER)

    assert response.status_code == 404, response.text
    assert _sent_delete(session) == [], 'a DELETE was sent for an id that does not exist'
    assert session.commits == 0


# ---------------------------------------------------------------------------------------------
# The two codes cannot be swapped, and neither leaks the stored owner
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('declared', ['someone-else', None], ids=['a-wrong-owner', 'no-owner-at-all'])
def test_an_unknown_id_is_404_even_when_the_owner_is_also_unacceptable(delete_entry, declared):
    """The ORDERING of the two mappings, which the two cases above cannot distinguish.

    ``_get_entry_or_raise`` runs before ``_check_owner``, so a missing id is 404 whatever the request
    declares. Both halves matter: a wrong owner must not promote a 404 into a 403 (which would tell a
    caller that an id they guessed exists), and an absent owner must not either -- the owner-less case
    answers 403 when the entry DOES exist, so 'owner-less means 403' is exactly the over-generalisation
    that would swap this.

    Args:
        delete_entry: Drives the real DELETE route against a fake session.
        declared: The owner the request declares, or None to send no owner parameter.
    """
    entry_id = uuid.uuid4()
    session = FakeSession(FakeResult(None))

    response = delete_entry(session, entry_id) if declared is None else delete_entry(session, entry_id, declared)

    assert response.status_code == 404, (
        f'a DELETE naming an id that does not exist answered {response.status_code} rather than 404: '
        f'{response.text}. The owner check ran in front of the existence check.'
    )


def test_the_403_body_never_names_the_real_owner(delete_entry):
    """Reads are open, but an error body is not a read endpoint (tj-vhboky.1 section 5).

    The route builds its detail from ``str(e)`` and OwnerMismatch refuses to format the owner, so the
    property holds at both layers. It is asserted HERE, on the wire, because the exception-level half
    is already pinned in test_dataset_entry_identity.py and the thing a caller sees is the response:
    a later 'helpful' detail string would leave that test green.
    """
    entry_id = uuid.uuid4()
    session = FakeSession(FakeResult(stored_entry(entry_id, owner='confidential-principal')))

    response = delete_entry(session, entry_id, 'someone-else')

    assert response.status_code == 403
    assert 'confidential-principal' not in response.text, f'the 403 body names the real owner: {response.text}'


# ---------------------------------------------------------------------------------------------
# The check itself, and the column constraint the whole design rests on
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('stored_owner', [OWNER, UNASSIGNED], ids=['a-normal-owner', 'the-migration-default'])
def test_the_owner_check_refuses_a_none_declaration(stored_owner: str):
    """``_check_owner`` directly, one layer below the route, for the same property.

    Worth having in ADDITION to the route case: this is the function two other id-addressed writes
    (``update_entry``, ``update_entry_lifecycle``) also go through, and neither has a route, so the
    route test cannot speak for them. A refactor that made None authorise would open all three.

    Args:
        stored_owner: The owner on the entry being checked against a None declaration.
    """
    entry = stored_entry(uuid.uuid4(), owner=stored_owner)

    with pytest.raises(crud.OwnerMismatch):
        crud._check_owner(entry, None)


def test_the_owner_column_refuses_null_and_says_what_depends_on_it():
    """THE CONSTRAINT THE 'None NEVER AUTHORISES' ARGUMENT RESTS ON, asserted rather than assumed.

    ``AssetDatasetStoreDelete.owner`` is optional so that an owner-less delete is refused by the
    handler instead of by validation. That is safe ONLY while ``existing.owner`` can never be None: a
    NULL owner makes ``existing.owner != declared_owner`` FALSE for a None declaration, and an
    owner-less DELETE starts succeeding against exactly those rows. Making this column nullable is the
    plausible change -- it reads like relaxing a constraint, not like removing an authorisation check.

    The server default is asserted for the other half of the same argument: rows created before
    owners existed have a non-NULL value because the migration (eec8f88a7443) supplied one, which is
    what let the column be added NOT NULL in a single step. Remove the default and the column can no
    longer be added NOT NULL without a separate backfill.
    """
    column = StoreDatasetEntry.__table__.columns['owner']

    assert column.nullable is False, (
        'store_dataset_entry.owner is nullable again. _check_owner compares existing.owner against '
        'the declared owner, and AssetDatasetStoreDelete.owner is optional -- so a NULL stored owner '
        'makes an owner-less DELETE AUTHORISE. See this file'
    )
    assert column.server_default is not None, (
        'store_dataset_entry.owner lost its server default, so the column can no longer be added '
        'NOT NULL in one step and pre-owner rows would need a separate backfill to stay non-NULL'
    )
