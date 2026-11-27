import pytest
from scim2_server.testing import AsyncScimStorageContract
from scim2_server.testing import ScimStorageContract

from .plain import PLAIN_MAPPINGS


class TestSqlAlchemyStorage(ScimStorageContract):
    @pytest.fixture
    def storage(self, sync_storage_factory):
        return sync_storage_factory()


class TestAsyncSqlAlchemyStorage(AsyncScimStorageContract):
    @pytest.fixture
    def storage(self, async_storage_factory):
        return async_storage_factory()


def one_work_email(user):
    emails = user.get("emails")
    if not emails:
        return user

    return user | {"emails": [{"value": emails[-1]["value"], "type": "work"}]}


class TestSqlAlchemyStorageWithHashes(ScimStorageContract):
    """Resources without dates nor version counter, whose emails are a work email held by the user record."""

    stores_created = False
    stores_last_modified = False

    def search_users(self):
        return [one_work_email(user) for user in super().search_users()]

    @pytest.fixture
    def storage(self, sync_storage_factory):
        return sync_storage_factory(PLAIN_MAPPINGS, version_key=b"secret")


class TestAsyncSqlAlchemyStorageWithHashes(AsyncScimStorageContract):
    stores_created = False
    stores_last_modified = False

    def search_users(self):
        return [one_work_email(user) for user in super().search_users()]

    @pytest.fixture
    def storage(self, async_storage_factory):
        return async_storage_factory(PLAIN_MAPPINGS, version_key=b"secret")
