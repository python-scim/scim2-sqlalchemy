import pytest
from scim2_models import EnterpriseUser
from scim2_models import SearchRequest
from scim2_models import User
from scim2_server.memory import InMemoryStorage

from .models import ENTERPRISE

UserModel = User[EnterpriseUser]

USERS = [
    {
        "userName": "alice",
        "displayName": "Alice",
        "title": "Boss",
        "name": {"givenName": "Alice", "familyName": "Liddell"},
        "active": True,
        "emails": [
            {"value": "alice@home.example", "type": "home"},
            {"value": "alice@work.example", "type": "work", "primary": True},
        ],
        ENTERPRISE: {"employeeNumber": "100"},
    },
    {
        "userName": "Bob",
        "active": False,
        "emails": [{"value": "bob@work.example", "type": "work"}],
    },
    {
        "userName": "carol",
        "title": "",
        "name": {"familyName": "smith"},
        ENTERPRISE: {"employeeNumber": "2"},
    },
    {
        "userName": "dave_%",
        "title": "50% off",
        "emails": [{"value": "dave@home.example"}],
    },
]

FILTERS = [
    'nonexistent eq "x"',
    "nonexistent pr",
    'nonexistent[value eq "x"]',
]
"""Filters on attributes the resource type does not declare, which a search
request bound to no model lets through. The filters of a bound request are
checked by the storage contract of scim2-server."""

SORTS = [
    "nonexistent",
    "password",
]
"""Sorts a bound search request refuses, and a request bound to no model lets
through."""


@pytest.fixture
def memory(user_type):
    storage = InMemoryStorage()
    for user in USERS:
        storage.create(user_type, UserModel.model_validate(user))
    return storage


@pytest.fixture
def sql(storage, user_type):
    """Return a storage holding the users of USERS."""
    for user in USERS:
        storage.create(user_type, UserModel.model_validate(user))
    return storage


def user_names(storage, user_type, **parameters):
    page = storage.search([user_type], SearchRequest(**parameters))
    names = [resource.user_name for resource in page.resources]
    assert page.total == len(names)
    return names


@pytest.mark.parametrize("scim_filter", FILTERS)
def test_a_filter_keeps_the_resources_kept_in_memory(
    memory, sql, user_type, scim_filter
):
    """The database keeps the resources that ScimFilter.match keeps."""
    expected = user_names(memory, user_type, filter=scim_filter)

    assert sorted(user_names(sql, user_type, filter=scim_filter)) == sorted(expected)


@pytest.mark.parametrize("sort_order", ["ascending", "descending"])
@pytest.mark.parametrize("sort_by", SORTS)
def test_a_sort_orders_the_resources_as_search_request_sort(
    sql, user_type, sort_by, sort_order
):
    """The database orders the resources as SearchRequest.sort does, ties by identifier."""
    search_request = SearchRequest(sort_by=sort_by, sort_order=sort_order)
    by_id = sql.search([user_type], SearchRequest()).resources

    assert user_names(sql, user_type, sort_by=sort_by, sort_order=sort_order) == [
        resource.user_name for resource in search_request.sort(by_id)
    ]


def test_a_page_is_cut_after_filtering_and_sorting(memory, sql, user_type):
    """The total counts every match, and the page holds count of them from startIndex."""
    parameters = {
        "filter": "userName pr",
        "sort_by": "userName",
        "start_index": 2,
        "count": 2,
    }
    page = sql.search([user_type], SearchRequest(**parameters))

    assert page.total == 4
    assert [r.user_name for r in page.resources] == ["Bob", "carol"]
