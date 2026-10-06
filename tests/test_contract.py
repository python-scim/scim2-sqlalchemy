import pytest
from scim2_server.testing import AsyncScimStorageContract
from scim2_server.testing import ScimStorageContract


class TestSqlAlchemyStorage(ScimStorageContract):
    supports_root_search = False

    @pytest.fixture
    def storage(self, sync_storage_factory):
        return sync_storage_factory()


class TestAsyncSqlAlchemyStorage(AsyncScimStorageContract):
    supports_root_search = False

    @pytest.fixture
    def storage(self, async_storage_factory):
        return async_storage_factory()
