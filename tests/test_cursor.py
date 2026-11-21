import datetime
import decimal
import uuid

import pytest
from scim2_models import Group
from scim2_models import InvalidCursorException
from scim2_models import SearchRequest
from scim2_models import User

from scim2_sqlalchemy.keyset import _decode
from scim2_sqlalchemy.keyset import _encode

from .models import RANKED
from .models import Ranked
from .models import UserRecord
from .models import groups_mapping
from .models import ranked_mapping


@pytest.fixture
def users(storage, user_type):
    """Create five users, two of them without a title."""
    for user_name, title in [
        ("alice", "b"),
        ("bob", None),
        ("carol", "a"),
        ("dave", "b"),
        ("erin", None),
    ]:
        storage.create(user_type, User(user_name=user_name, title=title))


def walk(storage, resource_types, search_request):
    """Follow the next cursors from the first page, and return the pages."""
    pages = [storage.search(resource_types, search_request)]
    while pages[-1].next is not None:
        pages.append(
            storage.search(resource_types, search_request, position=pages[-1].next)
        )
    return pages


def walk_back(storage, resource_types, search_request, page):
    """Follow the previous cursors from a page, and return the pages in the sort order."""
    pages = [page]
    while pages[0].previous is not None:
        pages.insert(
            0,
            storage.search(resource_types, search_request, position=pages[0].previous),
        )
    return pages


def user_names(pages):
    return [resource.user_name for page in pages for resource in page.resources]


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
def test_cursors_page_through_the_sort_and_back(users, storage, user_type, sort_order):
    """The pages follow the sort, users without a title included, in both directions."""
    search_request = SearchRequest(
        cursor="", count=2, sort_by="title", sort_order=sort_order
    )
    expected = user_names(
        [
            storage.search(
                [user_type], SearchRequest(sort_by="title", sort_order=sort_order)
            )
        ]
    )

    forward = walk(storage, [user_type], search_request)
    backward = walk_back(storage, [user_type], search_request, forward[-1])

    assert user_names(forward) == expected
    assert user_names(backward) == expected


@pytest.mark.parametrize("sort_by", ["meta.created", "id"])
def test_cursors_page_on_dates_and_identifiers(users, storage, user_type, sort_by):
    """A sort value that JSON cannot hold as it is still resumes the paging."""
    search_request = SearchRequest(cursor="", count=2, sort_by=sort_by)

    pages = walk(storage, [user_type], search_request)

    assert sorted(user_names(pages)) == ["alice", "bob", "carol", "dave", "erin"]


def test_a_cursor_without_count_returns_every_resource(users, storage, user_type):
    """Without count, the first page holds every resource, and no page follows."""
    page = storage.search([user_type], SearchRequest(cursor=""))

    assert len(page.resources) == 5
    assert page.next is None
    assert page.previous is None


def test_a_page_reached_forward_assumes_a_previous_page(users, storage, user_type):
    """A page after the first one gives a previous cursor, even when the resources before it are gone."""
    search_request = SearchRequest(cursor="", count=2)
    first, second, _ = walk(storage, [user_type], search_request)
    for resource in first.resources:
        storage.delete(user_type, resource.id)

    previous = storage.search([user_type], search_request, position=second.previous)

    assert second.previous is not None
    assert previous.resources == []
    assert previous.next is None
    assert previous.previous is None


@pytest.mark.parametrize("sort_by", [None, "displayName"])
def test_cursors_page_at_the_root(storage, user_type, group_type, sort_by):
    """A search at the root pages the resources of every type as one collection, in both directions."""
    for name in ["d", "b", "e"]:
        storage.create(user_type, User(user_name=name, display_name=name))
    for name in ["c", "a"]:
        storage.create(group_type, Group(display_name=name))
    resource_types = [user_type, group_type]
    search_request = SearchRequest(cursor="", count=2, sort_by=sort_by)
    expected = [
        resource.display_name
        for resource in storage.search(
            resource_types, SearchRequest(sort_by=sort_by)
        ).resources
    ]

    forward = walk(storage, resource_types, search_request)
    backward = walk_back(storage, resource_types, search_request, forward[-1])

    names = [[r.display_name for r in page.resources] for page in forward]
    assert [name for page in names for name in page] == expected
    assert [[r.display_name for r in page.resources] for page in backward] == names


@pytest.mark.parametrize(
    "position",
    [
        "next",
        {"d": "next", "i": "x"},
        {"d": "sideways", "k": None, "i": "x"},
        {"d": "next", "k": None, "i": 1},
        {"d": "next", "k": {"datetime": "never"}, "i": "x"},
        {"d": "next", "k": None, "i": "not an identifier"},
    ],
)
def test_a_malformed_position_is_an_invalid_cursor(storage, user_type, position):
    """A position the storage did not give raises invalidCursor."""
    with pytest.raises(InvalidCursorException):
        storage.search([user_type], SearchRequest(cursor="x"), position=position)


@pytest.mark.parametrize(
    "position",
    [
        {"d": "next", "k": None, "i": "x"},
        {"d": "next", "k": None, "t": True, "i": "x"},
        {"d": "next", "k": None, "t": 2, "i": "x"},
    ],
)
def test_a_malformed_root_position_is_an_invalid_cursor(
    storage, user_type, group_type, position
):
    """A position at the root needs the position of an existing resource type."""
    with pytest.raises(InvalidCursorException):
        storage.search(
            [user_type, group_type], SearchRequest(cursor="x"), position=position
        )


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        3,
        1.5,
        "text",
        datetime.datetime(2026, 10, 9, 12, tzinfo=datetime.UTC),
        datetime.date(2026, 10, 9),
        decimal.Decimal("1.50"),
        uuid.UUID(int=1),
    ],
)
def test_a_sort_value_survives_its_position(value):
    """A sort value is read back from its position in its own type."""
    assert _decode(_encode(value)) == value
    assert type(_decode(_encode(value))) is type(value)


def test_a_sort_value_of_an_unknown_type_cannot_be_paged():
    """A sort value JSON cannot hold is refused rather than written wrong."""
    with pytest.raises(TypeError):
        _encode(b"bytes")


@pytest.mark.parametrize(
    "value",
    [["a"], {"a": "1", "b": "2"}, {"bytes": "x"}, {"date": 1}, {"decimal": "x"}],
)
def test_a_malformed_sort_value_is_an_invalid_cursor(value):
    """A sort value that no position holds raises invalidCursor."""
    with pytest.raises(InvalidCursorException):
        _decode(value)


@pytest.fixture
def ranked(storage_factory, user_type, group_type):
    """Return a storage of a user ranked 1, a user without rank and a group."""
    storage = storage_factory(
        mappings={
            "User": ranked_mapping(UserRecord.badge_number),
            "Group": groups_mapping(),
        }
    )
    user = User[Ranked](user_name="ranked")
    user[Ranked] = Ranked(rank=1)
    storage.create(user_type, user)
    storage.create(user_type, User[Ranked](user_name="unranked"))
    storage.create(group_type, Group(display_name="admins"))
    return storage


@pytest.mark.parametrize(
    "value", [10**20, "alice", True], ids=["too large", "text", "boolean"]
)
@pytest.mark.parametrize("root", [False, True], ids=["one type", "root"])
def test_a_sort_value_the_column_cannot_hold_is_an_invalid_cursor(
    ranked, user_type, group_type, root, value
):
    """A sort value no column of integers holds, such as one from a former sort, is refused rather than sent to the database."""
    position = {"d": "next", "k": value, "i": str(uuid.uuid4())}
    resource_types = [user_type, group_type] if root else [user_type]
    if root:
        position["t"] = 0
    search_request = SearchRequest(cursor="x", sort_by=f"{RANKED}:rank")

    with pytest.raises(InvalidCursorException):
        ranked.search(resource_types, search_request, position=position)


@pytest.mark.parametrize("root", [False, True], ids=["one type", "root"])
def test_a_sort_value_beyond_its_column_reads_the_values_past_it(
    ranked, user_type, group_type, root
):
    """An integer larger than any value of the column is past them all, before the missing ones."""
    position = {"d": "next", "k": 3_000_000_000, "i": str(uuid.uuid4())}
    resource_types = [user_type, group_type] if root else [user_type]
    if root:
        position["t"] = 0
    search_request = SearchRequest(cursor="x", sort_by=f"{RANKED}:rank")

    page = ranked.search(resource_types, search_request, position=position)

    names = [
        r.user_name if r.meta.resource_type == "User" else r.display_name
        for r in page.resources
    ]
    assert sorted(names) == (["admins", "unranked"] if root else ["unranked"])


def test_cursors_page_on_a_column_converting_its_values(storage_factory, user_type):
    """The sort values of a column type converting them are compared through it."""
    storage = storage_factory(
        mappings={"User": ranked_mapping(UserRecord.rank), "Group": groups_mapping()}
    )
    for user_name, rank in [("b", 2), ("a", 1), ("c", 3)]:
        user = User[Ranked](user_name=user_name)
        user[Ranked] = Ranked(rank=rank)
        storage.create(user_type, user)
    search_request = SearchRequest(cursor="", count=1, sort_by=f"{RANKED}:rank")

    assert user_names(walk(storage, [user_type], search_request)) == ["a", "b", "c"]
