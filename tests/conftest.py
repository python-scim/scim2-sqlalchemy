import asyncio
import getpass
import shutil

import pytest
from pytest_mysql import factories
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_server.testing import BlockingStorage
from scim2_server.utils import load_default_provider
from scim2_server.utils import load_default_service_provider_config
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from scim2_sqlalchemy import AsyncSqlAlchemyStorage
from scim2_sqlalchemy import SqlAlchemyStorage

from .models import GROUPS
from .models import MAPPINGS
from .models import USERS
from .models import Base
from .plain import PLAIN_MAPPINGS  # noqa: F401  The tables of the plain records are created with the others.

DATABASES = (
    ["sqlite"]
    + (["postgresql"] if shutil.which("pg_ctl") else [])
    + (["mariadb"] if shutil.which("mariadbd") else [])
)
"""The databases of the tests: PostgreSQL and MariaDB too when their servers are installed."""

ASYNC_DRIVERS = {
    "sqlite://": "sqlite+aiosqlite://",
    "mariadb+pymysql://": "mariadb+asyncmy://",
}

mariadb_proc = factories.mysql_proc(
    mysqld_exec="mariadbd",
    admin_executable="mariadb-admin",
    mysqld_safe="mariadbd-safe",
    install_db="mariadb-install-db",
    user=getpass.getuser(),
)


def async_url(url):
    for driver, async_driver in ASYNC_DRIVERS.items():
        url = url.replace(driver, async_driver)
    return url


def sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite checks the foreign keys only when asked to."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


ON_CONNECT = {"sqlite": sqlite_foreign_keys}


def enforce_foreign_keys(engine):
    on_connect = ON_CONNECT.get(engine.dialect.name, lambda *args: None)
    event.listen(engine, "connect", on_connect)
    return engine


@pytest.fixture(scope="session")
def postgresql_url(postgresql_proc):
    """Return the URL of a database on the PostgreSQL server the tests start."""
    return (
        f"postgresql+psycopg://{postgresql_proc.user}@{postgresql_proc.host}"
        f":{postgresql_proc.port}/postgres"
    )


@pytest.fixture(scope="session")
def mariadb_url(mariadb_proc):
    """Return the URL of a database on the MariaDB server the tests start."""
    socket = f"unix_socket={mariadb_proc.unixsocket}"
    server = create_engine(f"mariadb+pymysql://{mariadb_proc.user}@localhost/?{socket}")
    with server.connect() as connection:
        connection.exec_driver_sql("CREATE DATABASE IF NOT EXISTS scim")
    server.dispose()
    return (
        f"mariadb+pymysql://{mariadb_proc.user}@localhost/scim?charset=utf8mb4&{socket}"
    )


@pytest.fixture(params=DATABASES)
def database_url(request, tmp_path):
    """Return the URL of an empty database, holding the tables of the test models."""
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path}/scim.sqlite"
    else:
        url = request.getfixturevalue(f"{request.param}_url")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


@pytest.fixture
def session(database_url):
    engine = enforce_foreign_keys(create_engine(database_url))
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def sync_storage_factory(session):
    """Build synchronous storages sharing the session of the test.

    The mixins come before the storage class, to override its methods.
    """

    def factory(mappings=MAPPINGS, mixins=(), provider=None, version_key=None):
        storage_class = type("Storage", (*mixins, SqlAlchemyStorage), {})
        return storage_class(
            mappings, lambda: session, provider=provider, version_key=version_key
        )

    return factory


@pytest.fixture
def async_storage_factory(database_url):
    """Build asynchronous storages sharing a session, whose coroutines run one after the other."""
    with asyncio.Runner() as runner:
        engine = create_async_engine(async_url(database_url), poolclass=NullPool)
        enforce_foreign_keys(engine.sync_engine)
        session = AsyncSession(engine)

        def factory(mappings=MAPPINGS, mixins=(), provider=None, version_key=None):
            storage_class = type("Storage", (*mixins, AsyncSqlAlchemyStorage), {})
            storage = storage_class(
                mappings, lambda: session, provider=provider, version_key=version_key
            )
            return BlockingStorage(storage, runner)

        yield factory
        runner.run(session.close())
        runner.run(engine.dispose())


@pytest.fixture(params=["sync", "async"])
def storage_factory(request, database_url):
    return request.getfixturevalue(f"{request.param}_storage_factory")


@pytest.fixture
def storage(storage_factory):
    return storage_factory()


@pytest.fixture
def provider() -> ScimProvider:
    return load_default_provider()


@pytest.fixture
def restricted_provider() -> ScimProvider:
    """Return a provider publishing the schemas of the test mappings, restricted to what they store."""
    return ScimProvider.from_discovery(
        [*USERS.schemas(), *GROUPS.schemas()],
        [
            ResourceType.from_resource(USERS.model),
            ResourceType.from_resource(GROUPS.model),
        ],
        config=load_default_service_provider_config(),
    )


@pytest.fixture
def user_type(provider) -> ResourceType:
    return next(rt for rt in provider.resource_types if rt.id == "User")


@pytest.fixture
def group_type(provider) -> ResourceType:
    return next(rt for rt in provider.resource_types if rt.id == "Group")
