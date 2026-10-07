import pytest
from scim2_models import Group
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import SearchRequest
from scim2_models import User
from sqlalchemy import event
from sqlalchemy.engine import Engine

from scim2_sqlalchemy import storage as storage_module

from .models import MAPPINGS
from .models import groups_mapping


@pytest.fixture
def everyone(storage, user_type, group_type):
    """Store two users and two groups, whose display names interleave."""
    storage.create(
        user_type,
        User(user_name="alice", display_name="b", emails=[{"value": "a@x.org"}]),
    )
    storage.create(user_type, User(user_name="bob", display_name="d", active=True))
    storage.create(group_type, Group(display_name="a"))
    storage.create(group_type, Group(display_name="c"))


def search(storage, resource_types, **parameters):
    return storage.search(resource_types, SearchRequest(**parameters))


def names(resources):
    return [resource.display_name for resource in resources]


def test_resources_of_several_types_sort_together(
    everyone, storage, user_type, group_type
):
    """A search at the root sorts the resources of every type as one collection."""
    total, resources = search(storage, [user_type, group_type], sort_by="displayName")

    assert total == 4
    assert names(resources) == ["a", "b", "c", "d"]


def test_resources_of_several_types_sort_in_descending_order(
    everyone, storage, user_type, group_type
):
    """The descending order applies to the united collection."""
    _, resources = search(
        storage,
        [user_type, group_type],
        sort_by="displayName",
        sort_order="descending",
    )

    assert names(resources) == ["d", "c", "b", "a"]


def test_a_page_at_the_root_spans_several_types(
    everyone, storage, user_type, group_type
):
    """The paging applies to the united collection, and the total counts every type."""
    total, resources = search(
        storage, [user_type, group_type], sort_by="displayName", start_index=2, count=2
    )

    assert total == 4
    assert names(resources) == ["b", "c"]


@pytest.mark.parametrize(
    ("sort_order", "expected"),
    [("ascending", ["b", "d", "a", "c"]), ("descending", ["a", "c", "d", "b"])],
)
def test_a_type_without_the_sort_attribute_has_no_value(
    everyone, storage, user_type, group_type, sort_order, expected
):
    """Groups have no userName, so they come last when ascending and first when descending."""
    _, resources = search(
        storage, [user_type, group_type], sort_by="userName", sort_order=sort_order
    )

    assert names(resources) == expected


def test_several_types_without_the_sort_attribute(
    everyone, storage_factory, user_type, group_type
):
    """Missing values of several types unite with the values of another type, whatever their SQL type."""
    team_type = group_type.model_copy(update={"id": "Team", "name": "Team"})
    storage = storage_factory({**MAPPINGS, "Team": groups_mapping()})

    total, resources = search(
        storage, [group_type, team_type, user_type], sort_by="active"
    )

    assert total == 6
    assert resources[0].user_name == "bob"


def test_pages_at_the_root_neither_overlap_nor_skip(
    everyone, storage, user_type, group_type
):
    """Without sortBy, the order is stable, even with identifiers of different types."""
    pages = [
        search(storage, [user_type, group_type], start_index=index, count=1)[1]
        for index in range(1, 5)
    ]

    assert sorted(names(page[0] for page in pages)) == ["a", "b", "c", "d"]


def test_a_filter_at_the_root_applies_to_every_type(
    everyone, storage, user_type, group_type
):
    """Each type evaluates the filter on its own attributes."""
    total, resources = search(
        storage,
        [user_type, group_type],
        filter='displayName lt "c" or userName eq "bob"',
        sort_by="displayName",
    )

    assert total == 3
    assert names(resources) == ["a", "b", "d"]


def test_an_attribute_a_type_declares_but_does_not_store_is_refused(
    everyone, storage, user_type, group_type
):
    """At the root as on one type, a filter on an attribute stored nowhere is an error."""
    with pytest.raises(InvalidFilterException):
        search(storage, [user_type, group_type], filter="profileUrl pr")


def test_a_sort_on_an_attribute_a_type_does_not_store_is_refused(
    everyone, storage, user_type, group_type
):
    """At the root as on one type, a sort on an attribute stored nowhere is an error."""
    with pytest.raises(InvalidPathException):
        search(storage, [user_type, group_type], sort_by="profileUrl")


def test_a_search_on_no_resource_type_finds_nothing(storage):
    """A server without resource types has nothing to search."""
    assert search(storage, []) == (0, [])


def test_the_root_only_loads_the_collections_the_response_keeps(
    everyone, storage, user_type, group_type
):
    """Each type follows the response parameters with its own attributes."""
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(Engine, "before_cursor_execute", record)
    try:
        _, resources = search(
            storage,
            [user_type, group_type],
            excluded_attributes=["emails", "members"],
        )
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    assert all(resource.meta is not None for resource in resources)
    assert not any("from emails" in statement for statement in statements)
    assert not any("from group_members" in statement for statement in statements)


def test_a_record_deleted_meanwhile_is_left_out(
    everyone, storage, user_type, group_type, monkeypatch
):
    """A record deleted between the selection of the page and its loading is skipped."""
    records_statement = storage_module._records_statement

    def without_the_first(mapping, ids, parameters):
        return records_statement(mapping, ids[1:], parameters)

    monkeypatch.setattr(storage_module, "_records_statement", without_the_first)
    total, resources = search(storage, [user_type, group_type], sort_by="displayName")

    assert total == 4
    assert names(resources) == ["c", "d"]
