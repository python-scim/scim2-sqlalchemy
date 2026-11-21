import pytest
from scim2_models import URN
from scim2_models import Extension
from scim2_models import Group
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import SearchRequest
from scim2_models import User
from sqlalchemy import event
from sqlalchemy.engine import Engine

from scim2_sqlalchemy import ResourceMapping
from scim2_sqlalchemy import storage as storage_module

from .models import MAPPINGS
from .models import RANKED
from .models import GroupRecord
from .models import Ranked
from .models import UserRecord
from .models import groups_mapping
from .models import ranked_mapping


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
    page = storage.search(resource_types, SearchRequest(**parameters))
    return page.total, page.resources


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


class TextRanked(Extension):
    """The ranked extension, with ranks held as text."""

    __schema__ = URN(RANKED)

    rank: str | None = None


def test_a_sort_on_values_of_different_types_is_refused(
    storage_factory, user_type, group_type
):
    """Users ranked by numbers and groups ranked by text cannot be sorted together."""
    groups = ResourceMapping(
        Group[TextRanked],
        GroupRecord,
        {
            "id": GroupRecord.id,
            f"{RANKED}:rank": GroupRecord.display_name,
            "meta.created": GroupRecord.created,
            "meta.lastModified": GroupRecord.last_modified,
        },
        version=GroupRecord.version,
    )
    storage = storage_factory(
        mappings={"User": ranked_mapping(UserRecord.badge_number), "Group": groups}
    )

    with pytest.raises(InvalidPathException, match="different types"):
        search(storage, [user_type, group_type], sort_by=f"{RANKED}:rank")


class NumberRanked(Extension):
    """The ranked extension, with ranks held as decimal numbers."""

    __schema__ = URN(RANKED)

    rank: float | None = None


@pytest.mark.parametrize("column", ["score", "weight"], ids=["float", "decimal"])
def test_a_sort_on_integers_and_decimals_mixes_them(
    storage_factory, user_type, group_type, column
):
    """Integers and decimal numbers sort together, and cursors page through them."""
    groups = ResourceMapping(
        Group[NumberRanked],
        GroupRecord,
        {
            "id": GroupRecord.id,
            "displayName": GroupRecord.display_name,
            f"{RANKED}:rank": getattr(GroupRecord, column),
            "meta.created": GroupRecord.created,
            "meta.lastModified": GroupRecord.last_modified,
        },
        version=GroupRecord.version,
    )
    storage = storage_factory(
        mappings={"User": ranked_mapping(UserRecord.badge_number), "Group": groups}
    )
    user = User[Ranked](user_name="two")
    user[Ranked] = Ranked(rank=2)
    storage.create(user_type, user)
    group = Group[NumberRanked](display_name="one and a half")
    group[NumberRanked] = NumberRanked(rank=1.5)
    storage.create(group_type, group)

    group = Group[NumberRanked](display_name="two and a half")
    group[NumberRanked] = NumberRanked(rank=2.5)
    storage.create(group_type, group)
    search_request = SearchRequest(cursor="", count=1, sort_by=f"{RANKED}:rank")

    pages = [storage.search([user_type, group_type], search_request)]
    while pages[-1].next is not None:
        pages.append(
            storage.search(
                [user_type, group_type], search_request, position=pages[-1].next
            )
        )

    assert [
        getattr(r, "user_name", None) or r.display_name
        for page in pages
        for r in page.resources
    ] == ["one and a half", "two", "two and a half"]


def test_a_whole_number_anchors_a_sort_on_decimal_numbers(storage_factory, group_type):
    """A database may return a decimal number without fraction as an integer, which a cursor keeps."""
    groups = ResourceMapping(
        Group[NumberRanked],
        GroupRecord,
        {
            "id": GroupRecord.id,
            "displayName": GroupRecord.display_name,
            f"{RANKED}:rank": GroupRecord.score,
            "meta.created": GroupRecord.created,
            "meta.lastModified": GroupRecord.last_modified,
        },
        version=GroupRecord.version,
    )
    storage = storage_factory(mappings={"Group": groups})
    for display_name, rank in [("two", 2.0), ("two and a half", 2.5)]:
        group = Group[NumberRanked](display_name=display_name)
        group[NumberRanked] = NumberRanked(rank=rank)
        storage.create(group_type, group)
    search_request = SearchRequest(cursor="x", sort_by=f"{RANKED}:rank")

    page = storage.search(
        [group_type], search_request, position={"d": "next", "k": 2, "i": "0"}
    )

    assert names(page.resources) == ["two", "two and a half"]
