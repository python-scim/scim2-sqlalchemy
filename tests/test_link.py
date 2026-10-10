import warnings

import pytest
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import InvalidFilterException
from scim2_models import InvalidValueException
from scim2_models import ResourceType
from scim2_models import ScimPolicy
from scim2_models import ScimProvider
from scim2_models import SearchRequest
from scim2_models import User
from scim2_server.utils import load_default_service_provider_config

from scim2_sqlalchemy import Link

from .models import GROUPS
from .models import USERS
from .models import GroupRecord
from .models import groups_mapping
from .models import users_mapping

UserModel = User[EnterpriseUser]


@pytest.fixture
def alice(storage, user_type):
    return storage.create(user_type, UserModel(user_name="alice", display_name="Alice"))


@pytest.fixture
def admins(storage, group_type):
    return storage.create(group_type, Group(display_name="admins"))


def display_names(storage, group_type, **parameters):
    page = storage.search([group_type], SearchRequest(**parameters))
    return sorted(resource.display_name for resource in page.resources)


def test_a_group_holds_users_and_groups(storage, group_type, alice, admins):
    """The members are read in the order of the Links, each with the type of its Link."""
    group = storage.create(
        group_type,
        Group(
            display_name="staff",
            members=[{"value": admins.id}, {"value": alice.id}],
        ),
    )

    assert [(m.value, m.type, m.display) for m in group.members] == [
        (alice.id, "User", "Alice"),
        (admins.id, "Group", "admins"),
    ]


def test_a_member_gets_the_ref_of_its_resource_type(
    storage_factory, restricted_provider, user_type, group_type
):
    """With a provider, each member gets the endpoint of its own resource type."""
    storage = storage_factory(provider=restricted_provider)
    alice = storage.create(user_type, UserModel(user_name="alice"))
    admins = storage.create(group_type, Group(display_name="admins"))

    group = storage.create(
        group_type, Group(members=[{"value": alice.id}, {"value": admins.id}])
    )

    assert [m.ref for m in group.members] == [
        f"Users/{alice.id}",
        f"Groups/{admins.id}",
    ]


@pytest.mark.parametrize("member_type", ["Group", "group"])
def test_a_member_with_the_type_of_its_resource_is_accepted(
    storage, group_type, admins, member_type
):
    """The type a client sends is compared as filters compare it, without its case."""
    group = storage.create(
        group_type, Group(members=[{"value": admins.id, "type": member_type}])
    )

    assert [(m.value, m.type) for m in group.members] == [(admins.id, "Group")]


def test_a_member_with_another_type_is_refused(storage, group_type, admins):
    """A group sent as a user raises a 400."""
    with pytest.raises(InvalidValueException, match="not of type 'User'"):
        storage.create(
            group_type, Group(members=[{"value": admins.id, "type": "User"}])
        )


def refusing_key(binding, value):
    """Fold the case, and refuse the strings holding an exclamation mark."""
    if "!" in value:
        raise ValueError("'!' cannot be compared")
    return value.casefold()


def test_a_type_the_policy_cannot_prepare_is_refused(
    storage_factory, user_type, group_type
):
    """A type the comparison key refuses matches no Link, and raises a 400, while the others are compared with the key."""
    provider = ScimProvider.from_discovery(
        [*USERS.schemas(), *GROUPS.schemas()],
        [
            ResourceType.from_resource(USERS.model),
            ResourceType.from_resource(GROUPS.model),
        ],
        config=load_default_service_provider_config(),
        policy=ScimPolicy(comparison_key=refusing_key),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        storage = storage_factory(provider=provider)
        admins = storage.create(group_type, Group(display_name="admins"))
        storage.create(
            group_type, Group(members=[{"value": admins.id, "type": "GROUP"}])
        )

        with pytest.raises(InvalidValueException):
            storage.create(
                group_type, Group(members=[{"value": admins.id, "type": "Group!"}])
            )


def test_a_member_sent_twice_is_stored_once(storage, group_type, alice):
    """Two entries with the same value link to the same user once."""
    group = storage.create(
        group_type, Group(members=[{"value": alice.id}, {"value": alice.id}])
    )

    assert [m.value for m in group.members] == [alice.id]


def test_a_group_can_hold_itself(storage, group_type, admins):
    """RFC 7643 forbids no cycle, and the storage reads the direct members only."""
    admins.members = [{"value": admins.id}]

    group = storage.update(group_type, admins)

    assert [(m.value, m.type) for m in group.members] == [(admins.id, "Group")]


def test_the_subgroups_are_replaced(storage, group_type, alice, admins):
    """Writing the members replaces the users and the groups."""
    group = storage.create(
        group_type,
        Group(display_name="staff", members=[{"value": admins.id}]),
    )
    group.members = [{"value": alice.id}]

    group = storage.update(group_type, group)

    assert [m.value for m in group.members] == [alice.id]


def test_the_groups_of_a_user_are_direct(storage, user_type, group_type, alice):
    """User.groups gets the type of its Link."""
    admins = storage.create(group_type, Group(members=[{"value": alice.id}]))

    groups = storage.get(user_type, alice.id).groups

    assert [(g.value, g.type) for g in groups] == [(admins.id, "direct")]


@pytest.mark.parametrize(
    ("scim_filter", "expected"),
    [
        ('members[type eq "Group"]', ["staff"]),
        ('members[type eq "user"]', ["admins", "staff"]),
        ('members.type eq "Group"', ["staff"]),
        ('members.type ne "Group"', ["admins", "empty"]),
        ('members[type eq "Group" and display eq "admins"]', ["staff"]),
        ("members.type pr", ["admins", "staff"]),
        ("members pr", ["admins", "staff"]),
        ("members[display pr]", ["admins", "staff"]),
        ('members.display eq "admins"', ["staff"]),
        ('members.value eq "{admins}"', ["staff"]),
        ('members.value eq "{alice}"', ["admins", "staff"]),
        ('members.value ne "{alice}"', ["empty"]),
    ],
)
def test_the_members_are_filtered_on_both_resource_types(
    storage, group_type, alice, admins, scim_filter, expected
):
    """A filter holds when a user or a group among the members matches it."""
    admins.members = [{"value": alice.id}]
    storage.update(group_type, admins)
    storage.create(
        group_type,
        Group(
            display_name="staff",
            members=[{"value": alice.id}, {"value": admins.id}],
        ),
    )
    storage.create(group_type, Group(display_name="empty"))
    scim_filter = scim_filter.format(alice=alice.id, admins=admins.id)

    assert display_names(storage, group_type, filter=scim_filter) == expected


def test_the_type_of_an_untyped_link_is_not_stored(
    storage_factory, user_type, group_type
):
    """Without a type on its Link, a member type sent by a client raises a 400, and filters refuse it."""
    groups = groups_mapping({"members": Link(GroupRecord.members, "User")})
    storage = storage_factory({"User": users_mapping(), "Group": groups})
    alice = storage.create(user_type, UserModel(user_name="alice"))

    with pytest.raises(InvalidValueException):
        storage.create(group_type, Group(members=[{"value": alice.id, "type": "User"}]))
    with pytest.raises(InvalidFilterException):
        display_names(storage, group_type, filter='members.type eq "User"')
