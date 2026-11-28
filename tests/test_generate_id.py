import asyncio
import uuid

import pytest
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import UniquenessException
from scim2_models import User
from scim2_server.testing import MAILBOX_TYPES
from scim2_server.testing import Mailbox

from .mailboxes import MAILBOX_MAPPINGS
from .models import GroupRecord
from .models import groups_mapping
from .models import users_mapping

UserModel = User[EnterpriseUser]

MAILBOX_TYPE, _ = MAILBOX_TYPES

BJENSEN = uuid.UUID("2819c223-7f76-453a-919d-413861904646")


class ChosenIds:
    """Return the identifiers of a list in order, then None."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []
        self.calls = 0

    def next_id(self):
        self.calls += 1
        return self.ids.pop(0) if self.ids else None


class SyncChosenIds(ChosenIds):
    def generate_id(self, resource_type, resource):
        return self.next_id()


class AsyncChosenIds(ChosenIds):
    async def generate_id(self, resource_type, resource):
        await asyncio.sleep(0)
        return self.next_id()


class FortyTwo(SyncChosenIds):
    def generate_id(self, resource_type, resource):
        return "42"


def test_a_new_user_gets_the_chosen_identifier(sync_storage_factory, user_type):
    """The UUID is stored, and comes back in lowercase."""
    storage = sync_storage_factory(mixins=[SyncChosenIds])
    storage.ids = [str(BJENSEN).upper()]

    user = storage.create(user_type, UserModel(user_name="bjensen"))

    assert user.id == str(BJENSEN)
    assert storage.get(user_type, str(BJENSEN)).user_name == "bjensen"


def test_an_asynchronous_storage_awaits_the_chosen_identifier(
    async_storage_factory, user_type
):
    """The asynchronous storage can wait for another service before it writes the resource."""
    storage = async_storage_factory(mixins=[AsyncChosenIds])
    storage.storage.ids = [str(BJENSEN)]

    user = storage.create(user_type, UserModel(user_name="bjensen"))

    assert user.id == str(BJENSEN)


def test_without_identifier_the_column_gives_one(
    storage_factory, user_type, group_type
):
    """Users get a random UUID, and groups get the next number of the database."""
    storage = storage_factory()

    user = storage.create(user_type, UserModel(user_name="bjensen"))
    group = storage.create(group_type, Group(display_name="Sales"))

    assert uuid.UUID(user.id)
    assert int(group.id)


def test_an_identifier_is_converted_to_the_type_of_its_column(
    sync_storage_factory, group_type
):
    """Groups have integer identifiers. The string 42 is stored as the number 42."""
    storage = sync_storage_factory(mixins=[FortyTwo])

    assert storage.create(group_type, Group(display_name="Sales")).id == "42"


def create_two_users_with_the_same_identifier(storage, chooser, user_type):
    chooser.ids = [str(BJENSEN), str(BJENSEN)]
    storage.create(user_type, UserModel(user_name="bjensen"))

    with pytest.raises(UniquenessException):
        storage.create(user_type, UserModel(user_name="babs"))


def test_a_taken_identifier_raises_a_409(sync_storage_factory, user_type):
    """Two users cannot have the same identifier."""
    storage = sync_storage_factory(mixins=[SyncChosenIds])

    create_two_users_with_the_same_identifier(storage, storage, user_type)


def test_an_asynchronous_storage_refuses_a_taken_identifier(
    async_storage_factory, user_type
):
    """Two users cannot have the same identifier."""
    storage = async_storage_factory(mixins=[AsyncChosenIds])

    create_two_users_with_the_same_identifier(storage, storage.storage, user_type)


def test_an_identifier_is_unique_across_resource_types(sync_storage_factory, user_type):
    """Per RFC 7643 §3.1, a mailbox cannot take the identifier of a user, though they are in different tables."""
    storage = sync_storage_factory(MAILBOX_MAPPINGS, mixins=[SyncChosenIds])
    storage.ids = [str(BJENSEN), str(BJENSEN)]
    storage.create(user_type, UserModel(user_name="bjensen"))

    with pytest.raises(UniquenessException):
        storage.create(MAILBOX_TYPE, Mailbox(display_name="Babs"))


def refuse_a_taken_user_name(storage, chooser, user_type):
    chooser.ids = [str(BJENSEN), str(uuid.uuid4())]
    storage.create(user_type, UserModel(user_name="bjensen"))

    with pytest.raises(UniquenessException):
        storage.create(user_type, UserModel(user_name="BJensen"))

    assert chooser.calls == 1


def test_a_refused_resource_gets_no_identifier(sync_storage_factory, user_type):
    """The storage checks the unique values first, so a refused user does not call generate_id."""
    storage = sync_storage_factory(mixins=[SyncChosenIds])

    refuse_a_taken_user_name(storage, storage, user_type)


def test_an_asynchronous_storage_chooses_no_identifier_for_a_refused_resource(
    async_storage_factory, user_type
):
    """A refused user does not call another service."""
    storage = async_storage_factory(mixins=[AsyncChosenIds])

    refuse_a_taken_user_name(storage, storage.storage, user_type)


def test_an_identifier_the_column_cannot_hold_is_refused(
    sync_storage_factory, user_type
):
    """Users have UUID identifiers, so bjensen is not one."""
    storage = sync_storage_factory(mixins=[SyncChosenIds])
    storage.ids = ["bjensen"]

    with pytest.raises(ValueError, match="'id' of User.* cannot be 'bjensen'"):
        storage.create(user_type, UserModel(user_name="bjensen"))


def test_an_identifier_the_storage_cannot_write_is_refused(
    sync_storage_factory, group_type
):
    """Here the identifier of the groups is a property without setter."""
    mappings = {
        "User": users_mapping(),
        "Group": groups_mapping({"id": GroupRecord.text_id}),
    }
    storage = sync_storage_factory(mappings, mixins=[FortyTwo])

    with pytest.raises(ValueError, match="cannot write the 'id' of Group"):
        storage.create(group_type, Group(display_name="Sales"))
