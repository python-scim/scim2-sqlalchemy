import unicodedata
import warnings

import pytest
from scim2_models import EnterpriseUser
from scim2_models import ResourceType
from scim2_models import ScimPolicy
from scim2_models import ScimProvider
from scim2_models import SearchRequest
from scim2_models import UniquenessException
from scim2_models import User
from scim2_server.utils import load_default_service_provider_config
from sqlalchemy.dialects import mysql

from scim2_sqlalchemy.comparison import _comparator
from scim2_sqlalchemy.query import _search_statements

from .models import GROUPS
from .models import USERS

UserModel = User[EnterpriseUser]


def strict_key(binding, value):
    """Fold the case fully, and refuse the strings holding an exclamation mark."""
    if "!" in value:
        raise ValueError("'!' cannot be compared")
    value = unicodedata.normalize("NFC", value)
    return value if binding.case_exact else value.casefold()


@pytest.fixture
def strict_provider() -> ScimProvider:
    """Return a provider whose policy compares strings with strict_key."""
    return ScimProvider.from_discovery(
        [*USERS.schemas(), *GROUPS.schemas()],
        [
            ResourceType.from_resource(USERS.model),
            ResourceType.from_resource(GROUPS.model),
        ],
        config=load_default_service_provider_config(),
        policy=ScimPolicy(comparison_key=strict_key),
    )


def user_names(storage, resource_type, **parameters):
    _, resources = storage.search([resource_type], SearchRequest(**parameters))
    return [resource.user_name for resource in resources]


def test_a_decomposed_value_matches_its_composed_form(storage, user_type):
    """Both sides are normalized to NFC, whatever form the value was written in."""
    storage.create(user_type, UserModel(user_name="Jose\u0301"))

    assert user_names(storage, user_type, filter='userName eq "José"') == ["Jose\u0301"]


def test_a_decomposed_value_is_taken(storage, user_type):
    """The uniqueness check compares the values in NFC."""
    storage.create(user_type, UserModel(user_name="José"))

    with pytest.raises(UniquenessException):
        storage.create(user_type, UserModel(user_name="Jose\u0301"))


@pytest.mark.parametrize(
    ("filter", "expected"),
    [
        ('externalId sw "AB"', []),
        ('externalId co "B"', []),
        ('externalId ew "BC"', []),
        ('externalId sw "ab"', ["bjensen"]),
        ('externalId co "b"', ["bjensen"]),
        ('externalId ew "bc"', ["bjensen"]),
    ],
)
def test_a_case_exact_attribute_keeps_its_case_in_substrings(
    storage, user_type, filter, expected
):
    """SQLite ignores the case of ASCII letters in LIKE, which a caseExact attribute must not."""
    storage.create(user_type, UserModel(user_name="bjensen", external_id="abc"))

    assert user_names(storage, user_type, filter=filter) == expected


@pytest.mark.parametrize("filter", ['userName ew "xbob"', 'userName sw "bobx"'])
def test_a_fragment_longer_than_the_value_matches_nothing(storage, user_type, filter):
    """A value cannot start or end with a longer string."""
    storage.create(user_type, UserModel(user_name="bob"))

    assert user_names(storage, user_type, filter=filter) == []


@pytest.mark.parametrize(
    ("filter", "expected"),
    [
        ('userName eq "a!"', []),
        ('userName ne "a!"', ["alice"]),
        ('userName co "!"', []),
        ('not (userName co "!")', ["alice"]),
        ('userName gt "a!"', []),
    ],
)
def test_an_operand_the_key_refuses_equals_nothing(
    storage_factory, strict_provider, user_type, filter, expected
):
    """A string the policy cannot prepare is equal to no other value, so only ne holds."""
    storage = storage_factory(provider=strict_provider)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        storage.create(user_type, UserModel(user_name="alice"))

        assert user_names(storage, user_type, filter=filter) == expected


def test_sqlite_compares_with_the_key_of_the_policy(
    storage_factory, strict_provider, user_type, database_url
):
    """SQLite calls the comparison key of the policy, so the comparison is exact."""
    if not database_url.startswith("sqlite"):
        pytest.skip("Only SQLite calls the comparison key of the policy")
    storage = storage_factory(provider=strict_provider)
    storage.create(user_type, UserModel(user_name="Straße"))

    assert user_names(storage, user_type, filter='userName eq "STRASSE"') == ["Straße"]
    with pytest.raises(UniquenessException):
        storage.create(user_type, UserModel(user_name="strasse"))


@pytest.fixture
def refused_value(storage_factory, user_type, database_url):
    """Store a user whose name the strict key refuses, between two others."""
    if not database_url.startswith("sqlite"):
        pytest.skip("Only SQLite calls the comparison key of the policy")
    for name in ("alice", "bad!", "zed"):
        storage_factory().create(user_type, UserModel(user_name=name))


@pytest.mark.parametrize(
    ("filter", "expected"),
    [('userName ne "alice"', ["bad!", "zed"]), ('userName co "a"', ["alice"])],
)
def test_a_stored_value_the_key_refuses_equals_nothing(
    refused_value, storage_factory, strict_provider, user_type, filter, expected
):
    """A stored string the policy cannot prepare matches ne only."""
    storage = storage_factory(provider=strict_provider)

    assert sorted(user_names(storage, user_type, filter=filter)) == expected


@pytest.mark.parametrize(
    ("order", "expected"),
    [("ascending", ["alice", "zed", "bad!"]), ("descending", ["bad!", "zed", "alice"])],
)
def test_a_stored_value_the_key_refuses_sorts_as_missing(
    refused_value, storage_factory, strict_provider, user_type, order, expected
):
    """A stored string the policy cannot prepare sorts with the missing values."""
    storage = storage_factory(provider=strict_provider)

    names = user_names(storage, user_type, sort_by="userName", sort_order=order)

    assert names == expected


def test_a_database_without_the_key_warns(
    storage_factory, strict_provider, user_type, database_url
):
    """Outside SQLite, a comparison key other than the default one is only approached."""
    storage = storage_factory(provider=strict_provider)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        user_names(storage, user_type, filter='userName eq "bjensen"')

    assert bool(caught) is not database_url.startswith("sqlite")


@pytest.mark.parametrize(
    ("filter", "expected"),
    [
        ('userName eq "A"', "lower(users.user_name) = lower('A')"),
        ('externalId eq "A"', "users.external_id = 'A'"),
        (
            'userName co "a%"',
            "lower(users.user_name) LIKE concat('%%', lower('a/%%'), '%%')",
        ),
    ],
)
def test_another_database_lowers_both_sides(filter, expected):
    """A database other than SQLite and PostgreSQL compares with its own lower()."""
    comparator = _comparator("mysql", ScimPolicy(), [USERS])
    count, _ = _search_statements(USERS, SearchRequest(filter=filter), comparator)

    sql = count.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})

    assert expected in str(sql)
