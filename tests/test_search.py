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
    'userName eq "ALICE"',
    'userName ne "alice"',
    'userName sw "dave_"',
    'userName ew "_%"',
    'userName co "a"',
    'userName gt "b"',
    'userName le "carol"',
    'title eq "boss"',
    'title ne "Boss"',
    'not (title eq "Boss")',
    "title eq null",
    "title ne null",
    "title gt null",
    "title pr",
    'title co "%"',
    'title lt "c"',
    "displayName pr",
    "name pr",
    "name.givenName pr",
    'name.familyName eq "SMITH"',
    "active eq true",
    "active eq false",
    "not (active eq true)",
    "active ne true",
    'meta.created co "20"',
    "active pr",
    "emails pr",
    'emails eq "alice@work.example"',
    'emails ne "alice@work.example"',
    'emails co "WORK"',
    'emails.type eq "work"',
    "emails.type pr",
    "emails.primary eq true",
    'emails[type eq "work" and value co "alice"]',
    'emails[type eq "home" and primary eq true]',
    'emails[value ne "alice@work.example"]',
    'emails[not (type eq "work")]',
    "emails[type pr]",
    'meta.created gt "2000-01-01T00:00:00Z"',
    'meta.lastModified lt "2000-01-01T00:00:00Z"',
    f'{ENTERPRISE}:employeeNumber eq "100"',
    f'{ENTERPRISE}:employeeNumber gt "10"',
    'title eq "Boss" or userName eq "Bob"',
    'title eq "Boss" and active eq false',
    'nonexistent eq "x"',
    "nonexistent pr",
    'nonexistent[value eq "x"]',
]

SORTS = [
    "userName",
    "title",
    "displayName",
    "name.familyName",
    "active",
    "emails",
    "emails.type",
    "meta.created",
    f"{ENTERPRISE}:employeeNumber",
    "nonexistent",
    "password",
]


@pytest.fixture
def memory(user_type):
    storage = InMemoryStorage()
    for user in USERS:
        storage.create(user_type, UserModel.model_validate(user))
    return storage


@pytest.fixture
def sql(storage, user_type):
    """Return a storage holding the users of USERS.

    Their identifiers increase, so that ties sort in creation order, as in memory.
    """
    for user in USERS:
        storage.create(user_type, UserModel.model_validate(user))
    return storage


def user_names(storage, user_type, **parameters):
    total, resources = storage.search([user_type], SearchRequest(**parameters))
    names = [resource.user_name for resource in resources]
    assert total == len(names)
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
def test_a_sort_orders_the_resources_as_in_memory(
    memory, sql, user_type, sort_by, sort_order
):
    """The database orders the resources as SearchRequest.sort does."""
    parameters = {"sort_by": sort_by, "sort_order": sort_order}

    assert user_names(sql, user_type, **parameters) == user_names(
        memory, user_type, **parameters
    )


def test_a_page_is_cut_after_filtering_and_sorting(memory, sql, user_type):
    """The total counts every match, and the page holds count of them from startIndex."""
    parameters = {
        "filter": "userName pr",
        "sort_by": "userName",
        "start_index": 2,
        "count": 2,
    }
    total, resources = sql.search([user_type], SearchRequest(**parameters))

    assert total == 4
    assert [r.user_name for r in resources] == ["Bob", "carol"]
