import datetime
import uuid

import pytest
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import InvalidValueException
from scim2_models import MutabilityException
from scim2_models import NotFoundException
from scim2_models import PreconditionFailedException
from scim2_models import SearchRequest
from scim2_models import UniquenessException
from scim2_models import User
from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping
from scim2_sqlalchemy import SqlAlchemyStorage
from scim2_sqlalchemy.storage import _translate

from .models import ENTERPRISE
from .models import RANKED
from .models import BadgeRecord
from .models import CertificateRecord
from .models import EmailRecord
from .models import GroupRecord
from .models import Ranked
from .models import UserRecord
from .models import groups_mapping
from .models import ranked_mapping
from .models import users_mapping

UserModel = User[EnterpriseUser]

TOO_LARGE = "99999999999999999999"
"""An integer no database stores."""

BEYOND_INTEGER = "3000000000"
"""An integer a BIGINT holds, but not the INTEGER of PostgreSQL."""


def search(storage, resource_type, **parameters):
    page = storage.search([resource_type], SearchRequest(**parameters))
    return page.total, page.resources


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


def test_a_binary_value_is_read_back_as_sent(storage_factory, user_type):
    """The column holds the decoded bytes, and the resource encodes them in base64 again."""
    certificates = Many(UserRecord.certificates, {"value": CertificateRecord.value})
    users = users_mapping({"x509Certificates": certificates})
    storage = storage_factory({"User": users, "Group": groups_mapping()})
    user = UserModel.model_validate(
        {"userName": "bjensen", "x509Certificates": [{"value": "TUlJRA=="}]}
    )

    user = storage.create(user_type, user)

    assert user.model_dump()["x509Certificates"] == [{"value": "TUlJRA=="}]


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
    """Locale comes from userName, and is filtered on as any attribute."""
    user = storage.create(user_type, UserModel(user_name="BJensen"))

    assert user.locale == "bjensen"
    _, found = search(storage, user_type, filter='locale eq "bjensen"')
    assert [r.user_name for r in found] == ["BJensen"]


def test_a_read_only_attribute_cannot_be_changed(storage, user_type):
    """A value the mapping cannot write raises a 400, rather than being lost."""
    with pytest.raises(MutabilityException):
        storage.create(user_type, UserModel(user_name="bjensen", locale="babs"))


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
    "value, expected",
    [
        ("{group_id}", ["alice"]),
        ("not-a-number", []),
        (TOO_LARGE, []),
        (BEYOND_INTEGER, []),
    ],
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


@pytest.mark.parametrize("group_id", ["not-a-number", TOO_LARGE, BEYOND_INTEGER])
def test_an_identifier_of_another_type_finds_nothing(storage, group_type, group_id):
    """A group identifier is an integer that its column holds, so another identifier is not found."""
    with pytest.raises(NotFoundException):
        storage.get(group_type, group_id)


@pytest.mark.parametrize("value", [TOO_LARGE, BEYOND_INTEGER])
@pytest.mark.parametrize("scim_filter", ['id eq "{}"', 'members.value eq "{}"'])
def test_an_integer_too_large_for_its_column_finds_nothing(
    storage, group_type, scim_filter, value
):
    """An integer the identifiers cannot hold finds no resource, rather than failing in the database."""
    storage.create(group_type, Group(display_name="admins"))

    total, _ = search(storage, group_type, filter=scim_filter.format(value))

    assert total == 0


def test_a_member_too_large_for_its_column_is_unknown(storage, group_type):
    """A group identifier no column holds links to no resource."""
    with pytest.raises(InvalidValueException, match="unknown resource"):
        storage.create(
            group_type, Group(display_name="admins", members=[{"value": TOO_LARGE}])
        )


@pytest.mark.parametrize(
    ("scim_filter", "expected"),
    [
        (f"rank eq {TOO_LARGE}", []),
        (f"rank ne {TOO_LARGE}", ["ranked", "unranked"]),
        (f"rank gt {TOO_LARGE}", []),
        (f"rank ge {TOO_LARGE}", []),
        (f"rank lt {TOO_LARGE}", ["ranked"]),
        (f"rank le {TOO_LARGE}", ["ranked"]),
        (f"rank gt -{TOO_LARGE}", ["ranked"]),
        (f"rank ge -{TOO_LARGE}", ["ranked"]),
        (f"rank lt -{TOO_LARGE}", []),
        (f"rank le -{TOO_LARGE}", []),
        (f"rank lt {BEYOND_INTEGER}", ["ranked"]),
        (f"rank eq {BEYOND_INTEGER}", []),
        (f"rank ne {BEYOND_INTEGER}", ["ranked", "unranked"]),
    ],
)
def test_an_integer_too_large_for_its_column_compares_as_a_number(
    storage_factory, user_type, scim_filter, expected
):
    """A number no column holds is greater or lower than any stored value."""
    storage = ranked_storage(storage_factory, user_type, UserRecord.badge_number)

    _, found = search(storage, user_type, filter=f"{RANKED}:{scim_filter}")

    assert sorted(resource.user_name for resource in found) == expected


@pytest.mark.parametrize(
    ("scim_filter", "expected"),
    [
        ("rank eq 1", ["ranked"]),
        ("rank gt 0", ["ranked"]),
    ],
)
def test_an_integer_is_compared_through_the_type_of_its_column(
    storage_factory, user_type, scim_filter, expected
):
    """A column type converting the values converts the compared value too."""
    storage = ranked_storage(storage_factory, user_type, UserRecord.rank)

    _, found = search(storage, user_type, filter=f"{RANKED}:{scim_filter}")

    assert [resource.user_name for resource in found] == expected


def ranked_storage(storage_factory, user_type, column):
    """Return a storage of a user ranked 1 and of a user without rank, the rank stored in column."""
    storage = storage_factory(mappings={"User": ranked_mapping(column)})
    ranked = User[Ranked](user_name="ranked")
    ranked[Ranked] = Ranked(rank=1)
    storage.create(user_type, ranked)
    storage.create(user_type, User[Ranked](user_name="unranked"))
    return storage


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


def test_read_only_entries_are_kept(storage_factory, user_type):
    """A Many whose sub-attributes are all read-only is only read: writing the user keeps its entries."""
    user = storage_factory().create(
        user_type, UserModel(user_name="a", emails=[{"value": "a@example.org"}])
    )
    emails = Many(
        UserRecord.emails, {"value": Attribute(EmailRecord.value, writable=False)}
    )
    users = users_mapping({"emails": emails})
    storage = storage_factory({"User": users, "Group": groups_mapping()})

    storage.update(user_type, storage.get(user_type, user.id))

    user = storage.get(user_type, user.id)
    assert [email.value for email in user.emails] == ["a@example.org"]
    user.emails[0].value = "b@example.org"
    with pytest.raises(MutabilityException):
        storage.update(user_type, user)


def test_an_entry_without_a_value_its_column_needs_is_refused(
    storage_factory, user_type
):
    """A certificate needs a value, while its type has a default."""
    certificates = Many(
        UserRecord.certificates,
        {"value": CertificateRecord.value, "type": CertificateRecord.type},
    )
    users = users_mapping({"x509Certificates": certificates})
    storage = storage_factory({"User": users, "Group": groups_mapping()})

    with pytest.raises(
        InvalidValueException, match="'.*:x509Certificates.value' is required"
    ):
        storage.create(
            user_type,
            UserModel(user_name="a", x509_certificates=[{"type": "work"}]),
        )

    user = storage.create(
        user_type,
        UserModel(user_name="b", x509_certificates=[{"value": "TUlJRA=="}]),
    )
    assert user.x509_certificates[0].type == "work"


def test_a_string_is_stored_in_an_integer_column_through_a_property(
    storage_factory, user_type
):
    """A property converts the badge number, and its expression casts it for filters."""
    users = users_mapping({"nickName": UserRecord.badge})
    storage = storage_factory({"User": users, "Group": groups_mapping()})

    user = storage.create(user_type, UserModel(user_name="a", nick_name="12"))

    assert storage.get(user_type, user.id).nick_name == "12"
    _, found = search(storage, user_type, filter='nickName sw "1"')
    assert [resource.user_name for resource in found] == ["a"]


class TakenMeanwhile:
    """Skip the uniqueness check, as when another transaction takes the value right after it."""

    def _unique_statements(self, mapping, resource, comparator, stored):
        return iter(())


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


class DriverError(Exception):
    """An error of a database driver, with the attributes the driver sets."""

    def __init__(self, *args, **attributes):
        super().__init__(*args)
        self.__dict__.update(attributes)


@pytest.mark.parametrize(
    "error",
    [
        DriverError("duplicate key", sqlstate="23505"),
        DriverError("duplicate key", pgcode="23505"),
        DriverError("UNIQUE failed", sqlite_errorname="SQLITE_CONSTRAINT_UNIQUE"),
        DriverError(1062, "Duplicate entry", sqlstate="23000"),
        DriverError("Duplicate entry", errno=1062, sqlstate="23000"),
    ],
    ids=["psycopg", "psycopg2", "sqlite3", "PyMySQL", "MariaDB Connector"],
)
def test_a_unique_value_taken_raises_a_409_whatever_the_driver(error):
    """Each driver tells a unique value taken its own way."""
    with pytest.raises(UniquenessException):
        _translate(IntegrityError("INSERT", {}, error))


def test_an_error_without_a_code_goes_through():
    """An error the storage cannot tell apart is left to the application."""
    _translate(IntegrityError("INSERT", {}, DriverError()))


def test_an_earlier_error_is_not_taken_for_the_cause():
    """An error the application was handling when it called the storage does not change the answer."""
    earlier = IntegrityError("INSERT", {}, DriverError(sqlstate="23505"))
    error = IntegrityError("INSERT", {}, DriverError(sqlstate="23502"))
    error.__context__ = earlier

    _translate(error)


def test_a_savepoint_gone_on_its_own_goes_through():
    """Only a savepoint gone after a record changed meanwhile is a 412."""
    _translate(OperationalError("ROLLBACK", {}, DriverError(1305, "SAVEPOINT")))


def test_a_deletion_the_database_refuses_raises(storage_factory, session, user_type):
    """A record outside SCIM can keep a resource from being deleted."""
    now = datetime.datetime.now(datetime.UTC)
    user_id = uuid.uuid4()
    session.add(
        UserRecord(id=user_id, user_name="bjensen", created=now, last_modified=now)
    )
    session.flush()
    session.add(BadgeRecord(user_id=user_id))
    session.commit()

    with pytest.raises(IntegrityError):
        storage_factory().delete(user_type, str(user_id))


@pytest.mark.parametrize(
    ("scim_filter", "found"),
    [
        ('id eq "{id}"', True),
        ('id eq "{hex}"', False),
        ('id eq "{upper}"', False),
        ('id eq "bjensen"', False),
        ('id ne "{id}"', False),
        ('id ne "bjensen"', True),
        ('id co "{part}"', True),
        ('id sw "{prefix}"', True),
        ('id co "-"', True),
        ('id co "{hex_part}"', False),
    ],
)
def test_a_uuid_identifier_matches_its_own_text(storage, user_type, scim_filter, found):
    """A UUID identifier is found by the text the storage returns, and by no other spelling."""
    user = storage.create(user_type, User(user_name="bjensen"))
    user_id = uuid.UUID(user.id)
    scim_filter = scim_filter.format(
        id=user.id,
        hex=user_id.hex,
        upper=user.id.upper(),
        part=user.id[9:13],
        prefix=user.id[:9],
        hex_part=user_id.hex[6:12],
    )

    total, _ = search(storage, user_type, filter=scim_filter)

    assert total == (1 if found else 0)


def test_an_unchanged_unique_value_is_not_checked_again(
    sync_storage_factory, session, user_type, database_url
):
    """Another application stored alice and Alice: Alice remains writable while she keeps her name."""
    if database_url.startswith("mariadb"):
        pytest.skip("The default collation of MariaDB cannot hold alice and Alice")
    now = datetime.datetime.now(datetime.UTC)
    for user_name in ("alice", "Alice"):
        session.add(UserRecord(user_name=user_name, created=now, last_modified=now))
    session.flush()
    storage = sync_storage_factory()
    _, found = search(storage, user_type)
    (alice,) = [user for user in found if user.user_name == "Alice"]
    alice.display_name = "Alice"

    assert storage.update(user_type, alice).display_name == "Alice"
    alice.user_name = "ALICE"
    with pytest.raises(UniquenessException):
        storage.update(user_type, alice)


def test_a_new_resource_with_the_id_of_another_is_checked(storage, user_type):
    """A new resource never is the record holding the identifier it carries."""
    bjensen = storage.create(user_type, UserModel(user_name="bjensen"))

    with pytest.raises(UniquenessException):
        storage.create(user_type, UserModel(id=bjensen.id, user_name="bjensen"))


def test_a_new_resource_with_the_id_of_another_gets_its_own(storage, user_type):
    """The storage never writes the identifier a new resource carries, so it clashes with nothing."""
    bjensen = storage.create(user_type, UserModel(user_name="bjensen"))

    created = storage.create(user_type, UserModel(id=bjensen.id, user_name="other"))

    assert created.id != bjensen.id


def test_a_boolean_stored_in_an_integer_column_is_filtered_on(
    storage_factory, user_type
):
    """A boolean compared with a column of integers is sent as 0 or 1."""
    users = users_mapping({"active": UserRecord.badge_number})
    storage = storage_factory({"User": users, "Group": groups_mapping()})
    storage.create(user_type, UserModel(user_name="a", active=True))
    storage.create(user_type, UserModel(user_name="b", active=False))

    _, found = search(storage, user_type, filter="active eq true")

    assert [resource.user_name for resource in found] == ["a"]


def test_a_resource_type_without_mapping_is_refused(session, provider):
    """A provider serving a resource type that no mapping stores is refused when the storage is built."""
    with pytest.raises(ValueError, match="No mapping for the resource type 'Group'"):
        SqlAlchemyStorage(
            {"User": users_mapping({"groups": None})},
            lambda: session,
            provider=provider,
        )


def test_a_change_of_links_only_changes_the_version(storage, user_type, group_type):
    """Adding a member only writes the association table, yet the group gets a new version."""
    bob = storage.create(user_type, User(user_name="bob"))
    group = storage.create(group_type, Group(display_name="Admins"))
    group.members = [{"value": bob.id}]

    updated = storage.update(group_type, group)

    assert updated.meta.version != group.meta.version
    assert updated.meta.last_modified > group.meta.last_modified


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


def changed_by_another_transaction(engine):
    """Return a mixin raising the version of the record in another transaction, right before the write."""

    class ChangedByAnotherTransaction:
        def _write_record(self, mapping, resource, record, links):
            with engine.begin() as connection:
                connection.execute(
                    update(UserRecord)
                    .where(UserRecord.id == record.id)
                    .values(version=UserRecord.version + 1)
                )
            super()._write_record(mapping, resource, record, links)

    return ChangedByAnotherTransaction


def test_a_version_another_transaction_changed_raises_a_412(
    storage_factory, database_url, user_type
):
    """MariaDB refuses to write a record changed since the transaction read it, which is a 412 too."""
    engine = create_engine(database_url)
    now = datetime.datetime.now(datetime.UTC)
    user_id = uuid.uuid4()
    with Session(engine) as other:
        other.add(
            UserRecord(id=user_id, user_name="bjensen", created=now, last_modified=now)
        )
        other.commit()
    storage = storage_factory(mixins=[changed_by_another_transaction(engine)])
    user = storage.get(user_type, str(user_id))
    user.display_name = "Barbara"

    with pytest.raises(PreconditionFailedException):
        storage.update(user_type, user)
    engine.dispose()


def test_an_identifier_read_as_a_string_over_integers_is_found(
    sync_storage_factory, group_type
):
    """A hybrid identifier may return a string in Python over an integer column in SQL."""
    groups = groups_mapping({"id": GroupRecord.text_id})
    storage = sync_storage_factory(mappings={"User": users_mapping(), "Group": groups})
    group = storage.create(group_type, Group(display_name="Admins"))

    assert storage.get(group_type, group.id).display_name == "Admins"


def test_an_identifier_is_found_by_its_exact_text(
    sync_storage_factory, session, user_type
):
    """A collation ignoring the case, such as the default one of MariaDB, finds no other identifier."""
    now = datetime.datetime.now(datetime.UTC)
    session.add(
        UserRecord(
            user_name="bjensen", display_name="bjensen", created=now, last_modified=now
        )
    )
    session.flush()
    mapping = users_mapping(
        {"id": UserRecord.user_name, "userName": UserRecord.display_name}
    )
    storage = sync_storage_factory(
        mappings={"User": mapping, "Group": groups_mapping()}
    )

    assert storage.get(user_type, "bjensen").id == "bjensen"
    with pytest.raises(NotFoundException):
        storage.get(user_type, "BJENSEN")


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
