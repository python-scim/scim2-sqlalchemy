import datetime

import pytest
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import InvalidValueException
from scim2_models import MutabilityException
from scim2_models import NotFoundException
from scim2_models import NotImplementedException
from scim2_models import PreconditionFailedException
from scim2_models import SearchRequest
from scim2_models import UniquenessException
from scim2_models import User
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import set_committed_value

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping

from .models import ENTERPRISE
from .models import BadgeRecord
from .models import EmailRecord
from .models import UserRecord
from .models import groups_mapping
from .models import users_mapping

UserModel = User[EnterpriseUser]


def search(storage, resource_type, **parameters):
    return storage.search([resource_type], SearchRequest(**parameters))


def test_emails_are_read_back_in_their_order(storage, user_type):
    """The entries of a Many keep their values and their order."""
    emails = [
        {"value": "b@example.org", "type": "home"},
        {"value": "a@example.org", "type": "work", "primary": True},
    ]
    user = storage.create(user_type, UserModel(user_name="bjensen", emails=emails))

    stored = storage.get(user_type, user.id)

    assert [(e.value, e.type, e.primary) for e in stored.emails] == [
        ("b@example.org", "home", None),
        ("a@example.org", "work", True),
    ]


def test_an_update_replaces_the_emails(sync_storage_factory, session, user_type):
    """Writing the resource deletes the former entries of a Many."""
    storage = sync_storage_factory()
    user = storage.create(
        user_type,
        UserModel(user_name="bjensen", emails=[{"value": "a@example.org"}]),
    )
    user.emails = [{"value": "b@example.org"}]

    storage.update(user_type, user)

    assert session.scalars(select(EmailRecord.value)).all() == ["b@example.org"]


def test_a_password_is_written_hashed_and_never_read(
    sync_storage_factory, session, user_type
):
    """A write-only attribute is written through its setter, and never comes back."""
    storage = sync_storage_factory()
    user = storage.create(user_type, UserModel(user_name="bjensen", password="secret"))

    assert user.password is None
    assert session.scalar(select(UserRecord.password_hash)) == "hashed:secret"


def test_an_update_without_password_keeps_the_password(
    sync_storage_factory, session, user_type
):
    """A resource never carries its password back, so its absence changes nothing."""
    storage = sync_storage_factory()
    user = storage.create(user_type, UserModel(user_name="bjensen", password="secret"))
    user.display_name = "Barbara"

    storage.update(user_type, user)

    assert session.scalar(select(UserRecord.password_hash)) == "hashed:secret"


def test_a_password_is_not_filtered_on(storage, user_type):
    """A write-only attribute cannot be compared."""
    with pytest.raises(InvalidFilterException):
        search(storage, user_type, filter='password eq "secret"')


def test_a_missing_value_keeps_the_default_of_the_column(storage, user_type):
    """A created resource without userType gets the default of the column, and an update can clear it."""
    user = storage.create(user_type, UserModel(user_name="bjensen"))
    assert user.user_type == "Employee"

    user.user_type = None
    assert storage.update(user_type, user).user_type is None


def test_a_value_whose_column_cannot_be_empty_cannot_be_removed(storage, user_type):
    """Removing userName would leave a NULL in a NOT NULL column, so it raises a 400."""
    user = storage.create(user_type, UserModel(user_name="bjensen"))
    user.user_name = None

    with pytest.raises(MutabilityException):
        storage.update(user_type, user)


def test_a_hybrid_property_without_setter_is_read_only(storage, user_type):
    """NickName comes from userName, and is filtered on as any attribute."""
    user = storage.create(user_type, UserModel(user_name="BJensen"))

    assert user.nick_name == "bjensen"
    _, found = search(storage, user_type, filter='nickName eq "bjensen"')
    assert [r.user_name for r in found] == ["BJensen"]


def test_a_read_only_attribute_cannot_be_changed(storage, user_type):
    """A value the mapping cannot write raises a 400, rather than being lost."""
    with pytest.raises(MutabilityException):
        storage.create(user_type, UserModel(user_name="bjensen", nick_name="babs"))


def test_a_read_resource_can_be_written_back(storage, user_type, group_type):
    """Read-only and derived values sent back unchanged are accepted."""
    alice = storage.create(
        user_type, UserModel(user_name="alice", display_name="Alice")
    )
    group = storage.create(
        group_type, Group(display_name="admins", members=[{"value": alice.id}])
    )

    storage.update(user_type, storage.get(user_type, alice.id))
    storage.update(group_type, storage.get(group_type, group.id))


@pytest.mark.parametrize(
    "attributes",
    [
        {"profile_url": "https://example.org/bjensen"},
        {"phone_numbers": [{"value": "555-1234"}]},
        {"name": {"middle_name": "Jane"}},
        {"emails": [{"value": "a@example.org", "display": "Work"}]},
        {ENTERPRISE: {"cost_center": "4130"}},
    ],
)
def test_an_attribute_stored_nowhere_cannot_be_written(storage, user_type, attributes):
    """A value no mapping stores raises a 400, rather than being lost."""
    with pytest.raises(InvalidValueException):
        storage.create(user_type, UserModel(user_name="bjensen", **attributes))


def test_a_read_only_attribute_of_the_schema_is_ignored(storage, user_type, group_type):
    """Per RFC 7644 §3.5.1, User.groups sent by a client is ignored."""
    group = storage.create(group_type, Group(display_name="admins"))

    user = storage.create(
        user_type, UserModel(user_name="bjensen", groups=[{"value": group.id}])
    )

    assert user.groups is None


def test_the_display_of_a_member_is_derived(storage, user_type, group_type):
    """A member displays the name of its user, whatever the client sent."""
    alice = storage.create(
        user_type, UserModel(user_name="alice", display_name="Alice")
    )

    group = storage.create(
        group_type,
        Group(display_name="admins", members=[{"value": alice.id, "display": "Bob"}]),
    )

    assert group.members[0].display == "Alice"


def test_the_type_of_a_member_is_not_stored(storage, user_type, group_type):
    """members.type is not mapped, so a value for it raises a 400."""
    alice = storage.create(user_type, UserModel(user_name="alice"))

    with pytest.raises(InvalidValueException):
        storage.create(
            group_type,
            Group(display_name="admins", members=[{"value": alice.id, "type": "User"}]),
        )


def test_a_group_links_to_its_members(storage, user_type, group_type):
    """A member is read back with the id and the displayName of the user."""
    alice = storage.create(
        user_type, UserModel(user_name="alice", display_name="Alice")
    )
    bob = storage.create(user_type, UserModel(user_name="bob"))

    group = storage.create(
        group_type,
        Group(display_name="admins", members=[{"value": alice.id}, {"value": bob.id}]),
    )

    assert sorted((m.value, m.display) for m in group.members) == sorted(
        [(alice.id, "Alice"), (bob.id, None)]
    )
    assert group.members[0].ref is None


def test_a_link_gets_a_ref_relative_to_the_scim_root(
    storage_factory, restricted_provider, user_type, group_type
):
    """With a provider, a member gets the endpoint of users and its id, which the server makes absolute."""
    storage = storage_factory(provider=restricted_provider)
    alice = storage.create(user_type, UserModel(user_name="alice"))

    group = storage.create(group_type, Group(members=[{"value": alice.id}]))

    assert group.members[0].ref == f"Users/{alice.id}"


def test_a_ref_sent_by_a_client_is_ignored(storage, user_type, group_type):
    """The $ref of a member is built from its value, so the one a client sends changes nothing."""
    alice = storage.create(user_type, UserModel(user_name="alice"))
    member = {"value": alice.id, "$ref": f"https://example.org/v2/Users/{alice.id}"}

    group = storage.create(group_type, Group(members=[member]))

    assert group.members[0].value == alice.id


@pytest.mark.parametrize(
    "member", [{"$ref": "https://example.org/v2/Users/1"}, {}], ids=["ref", "empty"]
)
def test_a_member_without_value_is_refused(storage, user_type, group_type, member):
    """A member is stored by its value, so a member without one raises a 400 rather than being lost."""
    with pytest.raises(InvalidValueException):
        storage.create(group_type, Group(display_name="admins", members=[member]))

    group = storage.create(group_type, Group(display_name="admins"))
    group.members = [member]
    with pytest.raises(InvalidValueException):
        storage.update(group_type, group)


def test_a_single_link_reads_the_linked_resource(
    storage_factory, restricted_provider, user_type
):
    """The manager of a user is read with the id, the URL and the display name of the manager."""
    storage = storage_factory(provider=restricted_provider)
    alice = storage.create(
        user_type, UserModel(user_name="alice", display_name="Alice")
    )

    bob = storage.create(
        user_type,
        UserModel(user_name="bob", **{ENTERPRISE: {"manager": {"value": alice.id}}}),
    )

    manager = bob[EnterpriseUser].manager
    assert (manager.value, manager.ref, manager.display_name) == (
        alice.id,
        f"Users/{alice.id}",
        "Alice",
    )


def test_a_single_link_is_filtered_on(storage, user_type):
    """A filter on the manager compares the linked record, and holds for a user without manager."""
    alice = storage.create(user_type, UserModel(user_name="alice"))
    storage.create(
        user_type,
        UserModel(user_name="bob", **{ENTERPRISE: {"manager": {"value": alice.id}}}),
    )

    for scim_filter, expected in [
        (f'{ENTERPRISE}:manager.value eq "{alice.id}"', ["bob"]),
        (f"{ENTERPRISE}:manager pr", ["bob"]),
        (f'not ({ENTERPRISE}:manager.value eq "{alice.id}")', ["alice"]),
        (f'{ENTERPRISE}:manager.value ne "{alice.id}"', ["alice"]),
    ]:
        _, found = search(storage, user_type, filter=scim_filter)
        assert [r.user_name for r in found] == expected, scim_filter


def test_a_single_link_is_removed(storage, user_type):
    """Writing a user without manager removes the link, and keeps the former manager.

    Bob has no other enterprise attribute, so the extension disappears with the manager.
    """
    alice = storage.create(user_type, UserModel(user_name="alice"))
    bob = storage.create(
        user_type,
        UserModel(user_name="bob", **{ENTERPRISE: {"manager": {"value": alice.id}}}),
    )
    bob[EnterpriseUser].manager = None

    assert storage.update(user_type, bob)[EnterpriseUser] is None
    assert storage.get(user_type, alice.id).user_name == "alice"


@pytest.mark.parametrize(
    "manager", [{"value": "unknown"}, {}], ids=["unknown", "empty"]
)
def test_a_single_link_must_name_an_existing_resource(storage, user_type, manager):
    """A manager that does not exist, or without value, raises a 400."""
    with pytest.raises(InvalidValueException):
        storage.create(
            user_type, UserModel(user_name="bob", **{ENTERPRISE: {"manager": manager}})
        )


def test_a_user_reads_its_groups(storage, user_type, group_type):
    """User.groups comes from the members of the groups."""
    alice = storage.create(user_type, UserModel(user_name="alice"))
    group = storage.create(
        group_type, Group(display_name="admins", members=[{"value": alice.id}])
    )

    groups = storage.get(user_type, alice.id).groups

    assert [(g.value, g.display) for g in groups] == [(group.id, "admins")]


def test_the_groups_of_a_user_are_not_written(storage, user_type, group_type):
    """User.groups is read-only: the groups are changed through their members."""
    alice = storage.create(user_type, UserModel(user_name="alice"))
    group = storage.create(
        group_type, Group(display_name="admins", members=[{"value": alice.id}])
    )
    alice = storage.get(user_type, alice.id)
    alice.groups = None

    storage.update(user_type, alice)

    assert [m.value for m in storage.get(group_type, group.id).members] == [alice.id]


def test_a_member_must_exist(storage, group_type):
    """A link to an unknown resource is refused."""
    with pytest.raises(InvalidValueException):
        storage.create(
            group_type, Group(display_name="admins", members=[{"value": "unknown"}])
        )


@pytest.mark.parametrize(
    "value, expected", [("{group_id}", ["alice"]), ("not-a-number", [])]
)
def test_filter_on_an_integer_identifier(
    storage, user_type, group_type, value, expected
):
    """An integer identifier compares as text, so any value can be compared to it."""
    alice = storage.create(user_type, UserModel(user_name="alice"))
    group = storage.create(
        group_type, Group(display_name="admins", members=[{"value": alice.id}])
    )
    scim_filter = f'groups.value eq "{value.format(group_id=group.id)}"'

    _, found = search(storage, user_type, filter=scim_filter)

    assert [r.user_name for r in found] == expected


def test_an_identifier_of_another_type_finds_nothing(storage, group_type):
    """A group identifier is an integer, so a text identifier is not found."""
    with pytest.raises(NotFoundException):
        storage.get(group_type, "not-a-number")


@pytest.mark.parametrize(
    "scim_filter",
    [
        'profileUrl eq "https://example.org"',
        'emails.display eq "x"',
        "addresses pr",
        "x509Certificates.value pr",
        'addresses[type eq "work"]',
    ],
)
def test_an_attribute_stored_nowhere_is_not_filtered_on(
    storage, user_type, scim_filter
):
    """Filtering after the query would make the total and the paging wrong."""
    with pytest.raises(InvalidFilterException):
        search(storage, user_type, filter=scim_filter)


@pytest.mark.parametrize("sort_by", ["profileUrl", "groups.display"])
def test_an_attribute_stored_nowhere_is_not_sorted_on(storage, user_type, sort_by):
    """Neither an unmapped attribute nor a link to another resource orders a search."""
    with pytest.raises(InvalidPathException):
        search(storage, user_type, sort_by=sort_by)


def test_a_search_at_the_root_is_not_supported(storage, user_type, group_type):
    """Several resource types live in several tables."""
    with pytest.raises(NotImplementedException):
        storage.search([user_type, group_type], SearchRequest())


def test_a_sort_without_primary_uses_the_first_entry(storage_factory, user_type):
    """Without a primary column, the first entry orders the resource."""
    users = ResourceMapping(
        User,
        UserRecord,
        {
            "id": UserRecord.id,
            "userName": UserRecord.user_name,
            "emails": Many(UserRecord.emails, {"value": EmailRecord.value}),
            "meta.created": UserRecord.created,
            "meta.lastModified": UserRecord.last_modified,
        },
        version=UserRecord.version,
    )
    storage = storage_factory({"User": users})
    storage.create(
        user_type,
        User(
            user_name="a",
            emails=[{"value": "z@example.org"}, {"value": "a@example.org"}],
        ),
    )
    storage.create(user_type, User(user_name="b", emails=[{"value": "m@example.org"}]))

    _, found = search(storage, user_type, sort_by="emails")

    assert [r.user_name for r in found] == ["b", "a"]


def test_a_link_without_display(storage_factory, user_type, group_type):
    """Links to groups whose mapping has no name have no display."""
    groups = groups_mapping({"displayName": None})
    storage = storage_factory({"User": users_mapping(), "Group": groups})
    alice = storage.create(user_type, UserModel(user_name="alice"))
    storage.create(group_type, Group(members=[{"value": alice.id}]))

    (group,) = storage.get(user_type, alice.id).groups

    assert group.display is None


def test_a_read_only_entry_column_is_read_only(storage_factory, user_type):
    """An Attribute without write function in a Many is only read."""
    users = ResourceMapping(
        User,
        UserRecord,
        {
            "id": UserRecord.id,
            "userName": UserRecord.user_name,
            "emails": Many(
                UserRecord.emails,
                {
                    "value": EmailRecord.value,
                    "display": Attribute(EmailRecord.value, writable=False),
                },
            ),
            "meta.created": UserRecord.created,
            "meta.lastModified": UserRecord.last_modified,
        },
        version=UserRecord.version,
    )
    storage = storage_factory({"User": users, "Group": groups_mapping()})
    user = storage.create(
        user_type, User(user_name="a", emails=[{"value": "a@example.org"}])
    )
    assert user.emails[0].display == "a@example.org"

    storage.update(user_type, user)
    user.emails[0].display = "other"
    with pytest.raises(MutabilityException):
        storage.update(user_type, user)


class TakenMeanwhile:
    """Skip the first uniqueness check, as when another transaction takes the value right after it."""

    checked = False

    def _unique_statements(self, mapping, resource):
        if not self.checked:
            self.checked = True
            return iter(())
        return super()._unique_statements(mapping, resource)


def test_a_value_taken_meanwhile_raises_a_409(storage_factory, user_type):
    """The unique constraint of the database catches what the check missed."""
    storage_factory().create(user_type, UserModel(user_name="bjensen"))
    storage = storage_factory(mixins=[TakenMeanwhile])

    with pytest.raises(UniquenessException):
        storage.create(user_type, UserModel(user_name="bjensen"))


def test_another_integrity_error_goes_through(storage_factory, user_type):
    """An integrity error unrelated to unique values is not a 409."""
    with pytest.raises(IntegrityError):
        storage_factory().create(user_type, UserModel())


def test_a_deletion_the_database_refuses_raises(storage_factory, session, user_type):
    """A record outside SCIM can keep a resource from being deleted."""
    now = datetime.datetime.now(datetime.UTC)
    session.add(UserRecord(id="1", user_name="bjensen", created=now, last_modified=now))
    session.flush()
    session.add(BadgeRecord(user_id="1"))
    session.commit()

    with pytest.raises(IntegrityError):
        storage_factory().delete(user_type, "1")


class ChangedMeanwhile:
    """Make the record look loaded at another version, as when another transaction changes it right before the write."""

    def _write_record(self, mapping, resource, record, links):
        set_committed_value(record, "version", 0)
        super()._write_record(mapping, resource, record, links)


def test_a_version_changed_meanwhile_raises_a_412(storage_factory, user_type):
    """The version column of the mapper catches what the version check missed."""
    storage = storage_factory(mixins=[ChangedMeanwhile])
    user = storage.create(user_type, UserModel(user_name="bjensen"))
    user.display_name = "Barbara"

    with pytest.raises(PreconditionFailedException):
        storage.update(user_type, user)


@pytest.mark.parametrize(
    "filter",
    ['userName eq "Élise"', 'userName sw "Éli"', 'userName ne "Élise"'],
)
def test_a_value_with_accents_matches_itself(storage, user_type, filter):
    """Both sides are lowered by the database, which may only lower ASCII letters."""
    storage.create(user_type, UserModel(user_name="Élise"))
    storage.create(user_type, UserModel(user_name="bjensen"))

    total, resources = search(storage, user_type, filter=filter)

    expected = ["bjensen"] if " ne " in filter else ["Élise"]
    assert [resource.user_name for resource in resources] == expected


def test_a_value_with_accents_is_taken(storage, user_type):
    """The uniqueness check finds a value with accents, before the database does."""
    storage.create(user_type, UserModel(user_name="Élise"))

    with pytest.raises(UniquenessException):
        storage.create(user_type, UserModel(user_name="Élise"))


def test_a_failed_operation_leaves_the_others(sync_storage_factory, session, user_type):
    """Each operation is a savepoint, so a bulk request goes on after a failure."""
    storage = sync_storage_factory()
    with storage.operation():
        storage.create(user_type, UserModel(user_name="alice"))
    with pytest.raises(UniquenessException), storage.operation():
        storage.create(user_type, UserModel(user_name="bob"))
        storage.create(user_type, UserModel(user_name="alice"))

    assert session.scalars(select(UserRecord.user_name)).all() == ["alice"]
    assert session.scalar(select(func.count()).select_from(UserRecord)) == 1
