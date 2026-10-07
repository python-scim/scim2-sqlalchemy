import pytest
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import ResponseParameters
from scim2_models import SearchRequest
from scim2_models import User
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .models import ENTERPRISE

UserModel = User[EnterpriseUser]


@pytest.fixture
def statements():
    """Record the SQL statements every engine runs during the test."""
    recorded = []

    def record(connection, cursor, statement, parameters, context, executemany):
        recorded.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    yield recorded
    event.remove(Engine, "before_cursor_execute", record)


@pytest.fixture
def bob(storage, user_type, group_type):
    """Store bob, with an email, a group and a manager."""
    alice = storage.create(user_type, UserModel(user_name="alice"))
    bob = storage.create(
        user_type,
        UserModel(
            user_name="bob",
            emails=[{"value": "bob@example.org"}],
            **{ENTERPRISE: {"manager": {"value": alice.id}}},
        ),
    )
    storage.create(
        group_type, Group(display_name="Admins", members=[{"value": bob.id}])
    )
    return bob


def search_bob(storage, user_type, statements, **parameters):
    """Return bob as a search returns him, and the statements the search ran."""
    statements.clear()
    _, resources = storage.search(
        [user_type], SearchRequest(filter='userName eq "bob"', **parameters)
    )
    return resources[0], [statement.lower() for statement in statements]


def loads(statements, table):
    return any(f"from {table}" in statement for statement in statements)


def test_a_search_loads_every_collection_by_default(
    bob, storage, user_type, statements
):
    """Without response parameters, the resources hold all their collections."""
    user, ran = search_bob(storage, user_type, statements)

    assert user.emails[0].value == "bob@example.org"
    assert user.groups[0].display == "Admins"
    assert user[EnterpriseUser].manager.value is not None
    assert loads(ran, "emails")


@pytest.mark.parametrize(
    "parameters",
    [{"excluded_attributes": ["emails"]}, {"attributes": ["userName"]}],
)
def test_a_collection_the_response_removes_is_not_loaded(
    bob, storage, user_type, statements, parameters
):
    """A storage may leave out what the response removes anyway, and saves the query."""
    user, ran = search_bob(storage, user_type, statements, **parameters)

    assert user.emails is None
    assert not loads(ran, "emails")


def test_a_requested_sub_attribute_loads_its_collection(
    bob, storage, user_type, statements
):
    """Requesting a sub-attribute keeps its collection in the response."""
    user, ran = search_bob(storage, user_type, statements, attributes=["emails.value"])

    assert user.emails[0].value == "bob@example.org"
    assert user.groups is None
    assert loads(ran, "emails")


def test_an_excluded_extension_does_not_load_its_links(
    bob, storage, user_type, statements
):
    """The manager belongs to the enterprise extension, which the response removes."""
    _, every = search_bob(storage, user_type, statements)
    user, ran = search_bob(
        storage, user_type, statements, excluded_attributes=[ENTERPRISE]
    )

    assert user[EnterpriseUser] is None or user[EnterpriseUser].manager is None
    assert len(ran) == len(every) - 1


def test_excluded_members_are_not_loaded(bob, storage, group_type, statements):
    """Excluding the members of a group saves loading them and their users."""
    statements.clear()
    _, groups = storage.search(
        [group_type], SearchRequest(excluded_attributes=["members"])
    )

    assert groups[0].members is None
    assert not loads([statement.lower() for statement in statements], "users")


def test_a_filter_on_an_excluded_collection_still_applies(
    bob, storage, user_type, statements
):
    """The filter runs in SQL, whether the response keeps the collection or not."""
    _, resources = storage.search(
        [user_type],
        SearchRequest(filter='emails co "bob"', excluded_attributes=["emails"]),
    )

    assert [user.user_name for user in resources] == ["bob"]


def test_a_read_skips_the_collections_the_response_removes(
    bob, storage, user_type, statements
):
    """Reading one resource honors the response parameters too."""
    statements.clear()
    user = storage.get(
        user_type,
        bob.id,
        response_parameters=ResponseParameters(excluded_attributes=["emails"]),
    )

    assert user.emails is None
    assert user.groups[0].display == "Admins"
    assert not loads([statement.lower() for statement in statements], "emails")


def test_a_read_without_parameters_loads_every_collection(bob, storage, user_type):
    """Writes read the whole resource, so a read without parameters keeps everything."""
    user = storage.get(user_type, bob.id)

    assert user.emails[0].value == "bob@example.org"
    assert user[EnterpriseUser].manager.value is not None
