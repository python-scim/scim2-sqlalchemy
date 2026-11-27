import pytest
from scim2_models import Group
from scim2_models import PreconditionFailedException
from scim2_models import SearchRequest

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Link
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping
from scim2_sqlalchemy import SqlAlchemyStorage

from .plain import PLAIN_MAPPINGS
from .plain import GeneratedVersionRecord
from .plain import PlainUserRecord
from .plain import plain_groups_mapping
from .plain import plain_users_mapping


def test_a_mapping_without_version_needs_a_version_key(session):
    with pytest.raises(ValueError, match="'User' has no version, so the storage needs"):
        SqlAlchemyStorage(PLAIN_MAPPINGS, lambda: session)


def test_a_mapping_without_version_refuses_an_empty_version_key(session):
    """An empty key would let anyone compute the version of hidden values."""
    with pytest.raises(ValueError, match="needs a version_key"):
        SqlAlchemyStorage(PLAIN_MAPPINGS, lambda: session, version_key=b"")


def test_a_version_the_database_generates_needs_a_modification_date():
    """Without a date, the storage counts the writes of links in the version itself."""
    with pytest.raises(ValueError, match="which the database cannot generate"):
        ResourceMapping(
            Group,
            GeneratedVersionRecord,
            {
                "id": GeneratedVersionRecord.id,
                "displayName": GeneratedVersionRecord.display_name,
            },
            version=GeneratedVersionRecord.version,
        )


def test_a_counter_changes_when_only_the_links_change(
    storage_factory, user_type, group_type, provider
):
    """Without a modification date, a write of the members alone still counts."""
    mappings = {
        "User": plain_users_mapping(),
        "Group": plain_groups_mapping(version=PLAIN_MAPPINGS["Group"].record.version),
    }
    storage = storage_factory(mappings, version_key=b"secret")
    user_model = provider.model_for(user_type)
    group_model = provider.model_for(group_type)
    alice = storage.create(user_type, user_model(user_name="alice"))
    group = storage.create(group_type, group_model(display_name="admins"))

    updated = storage.update(
        group_type,
        group_model(id=group.id, display_name="admins", members=[{"value": alice.id}]),
    )

    assert updated.meta.version != group.meta.version
    assert updated.meta.version.startswith('W/"')


def test_a_hash_version_refuses_an_outdated_write(storage_factory, user_type, provider):
    storage = storage_factory(PLAIN_MAPPINGS, version_key=b"secret")
    user_model = provider.model_for(user_type)
    alice = storage.create(user_type, user_model(user_name="alice"))
    storage.update(user_type, user_model(id=alice.id, user_name="alice", title="Boss"))

    with pytest.raises(PreconditionFailedException):
        storage.delete(user_type, alice.id, expected_version=alice.meta.version)


def test_an_entry_is_read_with_its_constants(storage_factory, user_type, provider):
    storage = storage_factory(PLAIN_MAPPINGS, version_key=b"secret")
    user_model = provider.model_for(user_type)
    storage.create(
        user_type,
        user_model(user_name="alice", emails=[{"value": "alice@example.com"}]),
    )

    (alice,) = storage.search(
        [user_type], SearchRequest(filter='emails[type eq "work"]')
    ).resources

    assert [(e.value, e.type, e.primary) for e in alice.emails] == [
        ("alice@example.com", "work", True)
    ]


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
def test_a_sort_on_a_constant_orders_the_users_without_entry_apart(
    storage_factory, user_type, provider, sort_order
):
    """A constant sorts as the value of the entry, and as no value without entry."""
    storage = storage_factory(PLAIN_MAPPINGS, version_key=b"secret")
    user_model = provider.model_for(user_type)
    storage.create(user_type, user_model(user_name="alice"))
    storage.create(
        user_type, user_model(user_name="bob", emails=[{"value": "bob@example.com"}])
    )
    search_request = SearchRequest(sort_by="emails.primary", sort_order=sort_order)
    everyone = storage.search([user_type], SearchRequest()).resources

    page = storage.search([user_type], search_request)

    assert [search_request.sort_value(r) for r in page.resources] == [
        search_request.sort_value(r) for r in search_request.sort(everyone)
    ]


def test_the_declarations_keep_their_relationship_and_expression():
    attribute = Attribute(PlainUserRecord.title, writable=False)
    many = Many(PlainUserRecord.groups, PlainUserRecord.title)
    link = Link(PlainUserRecord.groups, "Group")

    assert attribute.expression is PlainUserRecord.title
    assert many.relationship is PlainUserRecord.groups
    assert link.relationship is PlainUserRecord.groups


def test_a_write_counts_once(storage, user_type, provider):
    """The counter of a written record goes up by one, whatever changes."""
    user_model = provider.model_for(user_type)
    alice = storage.create(user_type, user_model(user_name="alice"))

    updated = storage.update(
        user_type, user_model(id=alice.id, user_name="alice", title="Boss")
    )

    assert (alice.meta.version, updated.meta.version) == ('W/"1"', 'W/"2"')
