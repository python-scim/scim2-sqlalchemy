import datetime
from typing import Annotated

import pytest
from scim2_models import URN
from scim2_models import CaseExact
from scim2_models import Extension
from scim2_models import Group
from scim2_models import GroupMember
from scim2_models import Mutability
from scim2_models import Returned
from scim2_models import Uniqueness
from scim2_models import UniquenessException
from scim2_models import User
from scim2_server.testing import MAILBOX_TYPES
from scim2_server.testing import Mailbox
from scim2_server.testing import MailboxAddress
from scim2_server.testing import MailboxOwner

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Link
from scim2_sqlalchemy import ResourceMapping

from .mailboxes import MAILBOX_MAPPINGS
from .mailboxes import MailboxRecord
from .mailboxes import MailboxView
from .mailboxes import SharedMailboxRecord
from .mailboxes import guarded_mailboxes_mapping
from .mailboxes import mailboxes_mapping
from .mailboxes import work_addresses_mapping
from .models import MAPPINGS
from .models import GroupRecord
from .models import UserRecord
from .models import users_mapping

MAILBOX_TYPE, SHARED_MAILBOX_TYPE = MAILBOX_TYPES

SEALED = "urn:example:params:scim:schemas:extension:sealed:2.0:User"


class Sealed(Extension):
    __schema__ = URN(SEALED)

    code: Annotated[str | None, Returned.never, Uniqueness.server] = None


class TeamMember(GroupMember):
    value: Annotated[
        str | None, Mutability.immutable, CaseExact.true, Uniqueness.server
    ] = None


class Team(Group):
    """A group whose members belong to no other team."""

    members: list[TeamMember] | None = None


def addresses(*values):
    return [MailboxAddress(value=value, type=type) for value, type in values]


def test_a_unique_value_hidden_from_clients_is_refused():
    """A 409 on the code would tell that another user holds it."""
    with pytest.raises(ValueError, match="hidden from clients"):
        ResourceMapping(
            User[Sealed],
            UserRecord,
            {
                "id": UserRecord.id,
                "userName": UserRecord.user_name,
                f"{SEALED}:code": UserRecord.nick_name,
            },
        )


def test_a_unique_index_of_the_entries_accepts_the_values_a_resource_keeps(
    storage_factory,
):
    """The old entries are deleted before the new ones are inserted, so the index of the application accepts a kept or swapped address."""
    storage = storage_factory(MAPPINGS | {"Mailbox": guarded_mailboxes_mapping()})
    mailbox = storage.create(
        MAILBOX_TYPE,
        Mailbox(
            display_name="Babs",
            addresses=addresses(
                ("babs@example.com", "work"), ("b@example.com", "home")
            ),
        ),
    )
    mailbox.display_name = "Barbara"
    mailbox = storage.update(MAILBOX_TYPE, mailbox)
    mailbox.addresses = addresses(
        ("babs@example.com", "home"), ("b@example.com", "work")
    )

    updated = storage.update(MAILBOX_TYPE, mailbox)

    assert updated.display_name == "Barbara"
    assert [(a.value, a.type) for a in updated.addresses] == [
        ("babs@example.com", "home"),
        ("b@example.com", "work"),
    ]


def test_a_unique_index_of_the_entries_refuses_a_value_a_resource_repeats(
    storage_factory,
):
    """The index of the application is stricter than SCIM, which lets a resource repeat its value."""
    storage = storage_factory(MAPPINGS | {"Mailbox": guarded_mailboxes_mapping()})

    with pytest.raises(UniquenessException):
        storage.create(
            MAILBOX_TYPE,
            Mailbox(
                addresses=addresses(
                    ("babs@example.com", "work"), ("babs@example.com", "home")
                )
            ),
        )


@pytest.mark.parametrize(
    "record", [MailboxRecord, MailboxView], ids=["same class", "same table"]
)
def test_a_resource_does_not_clash_with_itself_through_another_mapping_of_its_record(
    storage_factory, record
):
    """The shared mailboxes read the work address of the same records, so a mailbox taking its own work address does not clash with itself."""
    storage = storage_factory(
        MAPPINGS
        | {
            "Mailbox": mailboxes_mapping(),
            "SharedMailbox": work_addresses_mapping(record),
        }
    )
    shared = storage.create(
        SHARED_MAILBOX_TYPE,
        Mailbox(addresses=addresses(("team@example.com", "work"))),
    )
    mailbox = storage.get(MAILBOX_TYPE, shared.id)
    assert mailbox.addresses is None
    mailbox.addresses = addresses(("team@example.com", "work"))

    updated = storage.update(MAILBOX_TYPE, mailbox)

    assert [a.value for a in updated.addresses] == ["team@example.com"]
    other = storage.create(MAILBOX_TYPE, Mailbox(display_name="Other"))
    other.addresses = addresses(("team@example.com", "work"))
    with pytest.raises(UniquenessException):
        storage.update(MAILBOX_TYPE, other)


def test_a_unique_member_belongs_to_one_group(storage_factory, user_type, group_type):
    """The value of a Link is checked as any other unique value."""
    teams = ResourceMapping(
        Team,
        GroupRecord,
        {
            "id": GroupRecord.id,
            "displayName": GroupRecord.display_name,
            "members": Link(GroupRecord.members, "User", type="User"),
            "meta.created": GroupRecord.created,
            "meta.lastModified": GroupRecord.last_modified,
        },
        version=GroupRecord.version,
    )
    storage = storage_factory({"User": users_mapping(), "Group": teams})
    bjensen = storage.create(user_type, User(user_name="bjensen"))
    member = TeamMember(value=bjensen.id, type="User")
    team = storage.create(group_type, Team(display_name="Sales", members=[member]))
    team.display_name = "Marketing"

    assert storage.update(group_type, team).display_name == "Marketing"
    with pytest.raises(UniquenessException):
        storage.create(group_type, Team(display_name="Support", members=[member]))


def test_an_update_taking_the_value_of_another_resource_type_is_refused(
    storage_factory,
):
    """Per RFC 7643 erratum 8279, an update looks for its new values in the other resource types too."""
    storage = storage_factory(MAILBOX_MAPPINGS)
    mailbox = storage.create(MAILBOX_TYPE, Mailbox(display_name="Babs"))
    storage.create(
        SHARED_MAILBOX_TYPE, Mailbox(addresses=addresses(("team@example.com", "work")))
    )
    mailbox.addresses = addresses(("team@example.com", "work"))

    with pytest.raises(UniquenessException):
        storage.update(MAILBOX_TYPE, mailbox)


def test_a_unique_value_another_resource_type_only_reads_is_taken(
    sync_storage_factory, session
):
    """Another application stores the owners of the shared mailboxes, which clients read, so a mailbox cannot take one."""
    shared = ResourceMapping(
        Mailbox,
        SharedMailboxRecord,
        {
            "id": SharedMailboxRecord.id,
            "owner.value": Attribute(SharedMailboxRecord.owner, writable=False),
            "meta.created": SharedMailboxRecord.created,
            "meta.lastModified": SharedMailboxRecord.last_modified,
        },
        version=SharedMailboxRecord.version,
    )
    storage = sync_storage_factory(
        MAPPINGS | {"Mailbox": mailboxes_mapping(), "SharedMailbox": shared}
    )
    now = datetime.datetime.now(datetime.UTC)
    session.add(SharedMailboxRecord(owner="bjensen", created=now, last_modified=now))
    session.flush()

    with pytest.raises(UniquenessException):
        storage.create(MAILBOX_TYPE, Mailbox(owner=MailboxOwner(value="BJensen")))
