import datetime
import uuid
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import asynccontextmanager
from contextlib import contextmanager
from typing import Any

from scim2_models import InvalidValueException
from scim2_models import NotFoundException
from scim2_models import Path
from scim2_models import PreconditionFailedException
from scim2_models import Resource
from scim2_models import ResourceType
from scim2_models import ResponseParameters
from scim2_models import ScimPolicy
from scim2_models import ScimProvider
from scim2_models import SearchRequest
from scim2_models import UniquenessException
from scim2_server.storage import AsyncScimStorage
from scim2_server.storage import ScimStorage
from sqlalchemy import Select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from .comparison import _Comparator
from .comparison import _comparator
from .conversion import _check_storable
from .conversion import _from_scim
from .conversion import _link_ids
from .conversion import _to_scim
from .conversion import _version
from .mapping import ResourceMapping
from .mapping import _Collection
from .query import _as_key
from .query import _links_statement
from .query import _load_statement
from .query import _records_statement
from .query import _root_statements
from .query import _search_statements
from .query import _taken_statement


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _by_position(rows: Sequence[Any]) -> dict[int, list[str]]:
    """Group the identifiers of a page at the root by position of their resource type."""
    ids: dict[int, list[str]] = {}
    for position, record_id in rows:
        ids.setdefault(position, []).append(record_id)
    return ids


def _nothing() -> None:
    pass


async def _nothing_async() -> None:
    pass


class _StorageBase:
    """What the synchronous and asynchronous storages share: everything but the queries."""

    def __init__(
        self,
        mappings: Mapping[str, ResourceMapping],
        clock: Callable[[], datetime.datetime] | None,
        provider: ScimProvider | None,
    ) -> None:
        self.mappings = dict(mappings)
        self.clock = clock or _utcnow
        self.provider = provider
        self._policy = provider.policy if provider else ScimPolicy()
        self._comparators: dict[str, _Comparator] = {}
        self._endpoints = {
            str(resource_type.name): str(resource_type.endpoint).strip("/")
            for resource_type in (provider.resource_types if provider else ())
        }
        for mapping in self.mappings.values():
            mapping._bind(self.mappings)

    def generate_id(self, resource_type: ResourceType, resource: Resource[Any]) -> str:
        """Return the identifier of a new resource, when its column has no default.

        Override this method to get predictable identifiers.
        """
        return uuid.uuid4().hex

    def _mapping(self, resource_type: ResourceType) -> ResourceMapping:
        return self.mappings[resource_type.name]  # type: ignore[index]

    def _root_page(
        self,
        resource_types: list[ResourceType],
        rows: Sequence[Any],
        records: dict[tuple[int, str], Any],
        search_request: SearchRequest[Any],
    ) -> list[Resource[Any]]:
        """Build the resources of a page at the root, in the order of the page.

        records holds the loaded records by position of their resource type
        and identifier as text. A record deleted after the page was selected
        is left out.
        """
        return [
            self._to_scim(
                resource_types[position], records[position, record_id], search_request
            )
            for position, record_id in rows
            if (position, record_id) in records
        ]

    def _model(self, resource_type: ResourceType) -> type[Resource[Any]]:
        """Return the model of the resources of a resource type: the one of the provider, if any."""
        model = self.provider.model_for(resource_type) if self.provider else None
        return model or self._mapping(resource_type).model  # type: ignore[return-value]

    def _to_scim(
        self,
        resource_type: ResourceType,
        record: Any,
        parameters: ResponseParameters[Any] | None = None,
    ) -> Resource[Any]:
        """Build the resource a record stores, with the collections the response keeps."""
        mapping = self._mapping(resource_type)
        model = self._model(resource_type)
        collections = mapping._returned_collections(parameters)
        return _to_scim(
            mapping,
            record,
            resource_type.name,  # type: ignore[arg-type]
            model,
            self._endpoints,
            collections,
        )

    def _check_storable(
        self, resource_type: ResourceType, resource: Resource[Any], record: Any
    ) -> None:
        """Refuse a change the mapping cannot store. record is None for a new resource."""
        stored = None if record is None else self._to_scim(resource_type, record)
        _check_storable(self._mapping(resource_type), resource, stored)

    @staticmethod
    def _not_found(
        resource_type: ResourceType, resource_id: str | None
    ) -> NotFoundException:
        return NotFoundException(
            detail=f"{resource_type.name} {resource_id!r} not found"
        )

    @staticmethod
    def _check_version(
        mapping: ResourceMapping, record: Any, expected_version: str | None
    ) -> None:
        if (
            expected_version is not None
            and _version(mapping, record) != expected_version
        ):
            raise PreconditionFailedException

    def _comparator(self, dialect: str) -> _Comparator:
        """Return how a database compares strings, built once for each dialect."""
        if dialect not in self._comparators:
            self._comparators[dialect] = _comparator(
                dialect, self._policy, self.mappings.values()
            )
        return self._comparators[dialect]

    @staticmethod
    def _unique_statements(
        mapping: ResourceMapping, resource: Resource[Any], comparator: _Comparator
    ) -> Iterator[Select[Any]]:
        """Yield the statements finding another record holding a unique value of the resource.

        A missing value never clashes, as a SQL NULL does not.
        """
        for column in mapping._unique_columns():
            value = Path(column.binding.urn).get(resource, strict=False)
            if value is not None:
                yield _taken_statement(mapping, column, value, resource.id, comparator)

    @staticmethod
    def _check_links(
        collection: _Collection, ids: list[str], records: list[Any]
    ) -> None:
        if len(records) != len(set(ids)):
            raise InvalidValueException(
                detail=f"'{collection.binding.urn}' links to an unknown resource"
            )

    def _new_record(self, resource_type: ResourceType, resource: Resource[Any]) -> Any:
        """Build an empty record for a new resource, with its identifier and its dates.

        The identifier is generated only when the database does not fill it.
        The resource is written once the record is in the session, so that the
        records it links to do not pull a record the session does not know.
        """
        mapping = self._mapping(resource_type)
        record = mapping.record()
        id_column = mapping._id.expression.property.columns[0]
        if (
            id_column.default is None
            and id_column.server_default is None
            and id_column is not id_column.table.autoincrement_column
        ):
            generated = self.generate_id(resource_type, resource)
            key = _as_key(mapping._id, generated)
            if key is None:
                raise ValueError(
                    f"The identifier {generated!r} does not fit the column "
                    f"of '{mapping._id.key}'"
                )
            setattr(record, mapping._id.key, key)
        now = self.clock()
        setattr(record, mapping._created.key, now)
        setattr(record, mapping._last_modified.key, now)
        return record

    def _write_record(
        self,
        mapping: ResourceMapping,
        resource: Resource[Any],
        record: Any,
        links: dict[_Collection, list[Any]],
    ) -> None:
        _from_scim(mapping, resource, record, links, creating=False)
        setattr(record, mapping._last_modified.key, self.clock())


class SqlAlchemyStorage(_StorageBase, ScimStorage):
    """Store SCIM resources in the records of SQLAlchemy models.

    The storage never commits: committing the request is left to the
    application. Each write is :ref:`flushed <sqlalchemy:session_flushing>`, so that the database fills the
    identifiers and checks its constraints.

    A change the mappings cannot store raises an error rather than being lost:
    :class:`~scim2_models.InvalidValueException` for an attribute no mapping
    stores, and :class:`~scim2_models.MutabilityException` for an attribute
    the mapping only reads. A value equal to the stored one is accepted, so a
    resource read and sent back can always be written.

    :param mappings: The mapping of each resource type, by resource type name.
    :param session: Return the current :class:`~sqlalchemy.orm.Session`, such
        as a :class:`~sqlalchemy.orm.scoped_session`.
    :param clock: Return the date of a write, for ``meta.created`` and
        ``meta.lastModified``. The current date in UTC by default.
    :param provider: The provider of the server. The storage then returns
        instances of its models, rather than of the models of the mappings,
        and gives each link a ``$ref`` relative to the SCIM root, from the
        endpoints of its resource types. Strings are compared with the
        :attr:`~scim2_models.ScimPolicy.comparison_key` of its policy.
    """

    def __init__(
        self,
        mappings: Mapping[str, ResourceMapping],
        session: Callable[[], Session],
        clock: Callable[[], datetime.datetime] | None = None,
        provider: ScimProvider | None = None,
    ) -> None:
        super().__init__(mappings, clock, provider)
        self.session = session

    @contextmanager
    def operation(self) -> Generator[None]:
        """Run the operation in a savepoint.

        The savepoint comes from :meth:`~sqlalchemy.orm.Session.begin_nested`.
        A failed operation of a bulk request is then rolled back alone
        (:rfc:`RFC 7644 §3.7 <7644#section-3.7>`).
        """
        with self.session().begin_nested():
            yield

    def get(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        response_parameters: ResponseParameters[Any] | None = None,
    ) -> Resource[Any]:
        record = self._load(resource_type, resource_id, response_parameters)
        return self._to_scim(resource_type, record, response_parameters)

    def search(
        self, resource_types: list[ResourceType], search_request: SearchRequest[Any]
    ) -> tuple[int, list[Resource[Any]]]:
        if not resource_types:
            return 0, []
        session = self.session()
        comparator = self._ready_comparator(session)
        mappings = [self._mapping(resource_type) for resource_type in resource_types]
        if len(mappings) == 1:
            count, page = _search_statements(mappings[0], search_request, comparator)
            total = session.scalar(count) or 0
            found = session.scalars(page).all()
            return total, [
                self._to_scim(resource_types[0], record, search_request)
                for record in found
            ]

        count, statement = _root_statements(mappings, search_request, comparator)
        total = session.scalar(count) or 0
        rows = session.execute(statement).all()
        records = {}
        for position, ids in _by_position(rows).items():
            loading = _records_statement(mappings[position], ids, search_request)
            for record_id, record in session.execute(loading):
                records[position, record_id] = record
        return total, self._root_page(resource_types, rows, records, search_request)

    def create(
        self, resource_type: ResourceType, resource: Resource[Any]
    ) -> Resource[Any]:
        mapping = self._mapping(resource_type)
        self._check_storable(resource_type, resource, None)
        self._check_uniqueness(mapping, resource)
        links = self._links(mapping, resource)
        record = self._new_record(resource_type, resource)

        def change() -> None:
            self.session().add(record)
            _from_scim(mapping, resource, record, links, creating=True)

        self._flush(change, lambda: self._check_uniqueness(mapping, resource))
        return self.get(resource_type, str(getattr(record, mapping._id.key)))

    def update(
        self,
        resource_type: ResourceType,
        resource: Resource[Any],
        *,
        expected_version: str | None = None,
    ) -> Resource[Any]:
        mapping = self._mapping(resource_type)
        record = self._load(resource_type, resource.id)
        self._check_version(mapping, record, expected_version)
        self._check_storable(resource_type, resource, record)
        self._check_uniqueness(mapping, resource)
        links = self._links(mapping, resource)
        self._flush(
            lambda: self._write_record(mapping, resource, record, links),
            lambda: self._check_uniqueness(mapping, resource),
        )
        return self.get(resource_type, resource.id)  # type: ignore[arg-type]

    def delete(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected_version: str | None = None,
    ) -> None:
        mapping = self._mapping(resource_type)
        record = self._load(resource_type, resource_id)
        self._check_version(mapping, record, expected_version)
        self._flush(lambda: self.session().delete(record))

    def _load(
        self,
        resource_type: ResourceType,
        resource_id: str | None,
        parameters: ResponseParameters[Any] | None = None,
    ) -> Any:
        """Load a record, with the collections the response keeps."""
        mapping = self._mapping(resource_type)
        statement = _load_statement(mapping, resource_id, parameters)
        record = None if statement is None else self.session().scalar(statement)
        if record is None:
            raise self._not_found(resource_type, resource_id)
        return record

    def _check_uniqueness(
        self, mapping: ResourceMapping, resource: Resource[Any]
    ) -> None:
        session = self.session()
        comparator = self._ready_comparator(session)
        for statement in self._unique_statements(mapping, resource, comparator):
            if session.scalar(statement) is not None:
                raise UniquenessException

    def _ready_comparator(self, session: Session) -> _Comparator:
        """Return how the database of the session compares strings, ready to compare them."""
        connection = session.connection()
        comparator = self._comparator(connection.dialect.name)
        comparator.prepare(connection.connection.driver_connection)
        return comparator

    def _links(
        self, mapping: ResourceMapping, resource: Resource[Any]
    ) -> dict[_Collection, list[Any]]:
        links = {}
        for collection, ids in _link_ids(mapping, resource).items():
            statement = _links_statement(collection, ids)
            records = list(self.session().scalars(statement).all())
            self._check_links(collection, ids, records)
            links[collection] = records
        return links

    def _flush(
        self,
        change: Callable[[], None],
        check_uniqueness: Callable[[], None] = _nothing,
    ) -> None:
        """Apply a change in a savepoint, and flush it.

        A version changed in the meantime by another transaction raises a 412.
        On an integrity error, check_uniqueness runs again, so that a unique
        value taken in the meantime raises a 409.
        """
        session = self.session()
        try:
            with session.begin_nested():
                change()
                session.flush()
        except StaleDataError as exception:
            raise PreconditionFailedException from exception
        except IntegrityError:
            check_uniqueness()
            raise


class AsyncSqlAlchemyStorage(_StorageBase, AsyncScimStorage):
    """The asynchronous variant of :class:`SqlAlchemyStorage`.

    It needs the ``asyncio`` extra, which installs the asynchronous support of
    SQLAlchemy: ``pip install "scim2-sqlalchemy[asyncio]"``.

    :param mappings: The mapping of each resource type, by resource type name.
    :param session: Return the current
        :class:`~sqlalchemy.ext.asyncio.AsyncSession`, such as an
        :class:`~sqlalchemy.ext.asyncio.async_scoped_session`.
    :param clock: Return the date of a write, for ``meta.created`` and
        ``meta.lastModified``. The current date in UTC by default.
    :param provider: The provider of the server. The storage then returns
        instances of its models, rather than of the models of the mappings.
        Strings are compared with the
        :attr:`~scim2_models.ScimPolicy.comparison_key` of its policy.
    """

    def __init__(
        self,
        mappings: Mapping[str, ResourceMapping],
        session: Callable[[], AsyncSession],
        clock: Callable[[], datetime.datetime] | None = None,
        provider: ScimProvider | None = None,
    ) -> None:
        super().__init__(mappings, clock, provider)
        self.session = session

    @asynccontextmanager
    async def operation(self) -> AsyncGenerator[None]:
        """Run the operation in a savepoint. See :meth:`SqlAlchemyStorage.operation`."""
        async with self.session().begin_nested():
            yield

    async def get(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        response_parameters: ResponseParameters[Any] | None = None,
    ) -> Resource[Any]:
        record = await self._load(resource_type, resource_id, response_parameters)
        return self._to_scim(resource_type, record, response_parameters)

    async def search(
        self, resource_types: list[ResourceType], search_request: SearchRequest[Any]
    ) -> tuple[int, list[Resource[Any]]]:
        if not resource_types:
            return 0, []
        session = self.session()
        comparator = await self._ready_comparator(session)
        mappings = [self._mapping(resource_type) for resource_type in resource_types]
        if len(mappings) == 1:
            count, page = _search_statements(mappings[0], search_request, comparator)
            total = await session.scalar(count) or 0
            found = (await session.scalars(page)).all()
            return total, [
                self._to_scim(resource_types[0], record, search_request)
                for record in found
            ]

        count, statement = _root_statements(mappings, search_request, comparator)
        total = await session.scalar(count) or 0
        rows = (await session.execute(statement)).all()
        records = {}
        for position, ids in _by_position(rows).items():
            loading = _records_statement(mappings[position], ids, search_request)
            for record_id, record in await session.execute(loading):
                records[position, record_id] = record
        return total, self._root_page(resource_types, rows, records, search_request)

    async def create(
        self, resource_type: ResourceType, resource: Resource[Any]
    ) -> Resource[Any]:
        mapping = self._mapping(resource_type)
        self._check_storable(resource_type, resource, None)
        await self._check_uniqueness(mapping, resource)
        links = await self._links(mapping, resource)
        record = self._new_record(resource_type, resource)

        async def change() -> None:
            self.session().add(record)
            _from_scim(mapping, resource, record, links, creating=True)

        await self._flush(change, lambda: self._check_uniqueness(mapping, resource))
        return await self.get(resource_type, str(getattr(record, mapping._id.key)))

    async def update(
        self,
        resource_type: ResourceType,
        resource: Resource[Any],
        *,
        expected_version: str | None = None,
    ) -> Resource[Any]:
        mapping = self._mapping(resource_type)
        record = await self._load(resource_type, resource.id)
        self._check_version(mapping, record, expected_version)
        self._check_storable(resource_type, resource, record)
        await self._check_uniqueness(mapping, resource)
        links = await self._links(mapping, resource)

        async def change() -> None:
            self._write_record(mapping, resource, record, links)

        await self._flush(change, lambda: self._check_uniqueness(mapping, resource))
        return await self.get(resource_type, resource.id)  # type: ignore[arg-type]

    async def delete(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected_version: str | None = None,
    ) -> None:
        mapping = self._mapping(resource_type)
        record = await self._load(resource_type, resource_id)
        self._check_version(mapping, record, expected_version)

        async def change() -> None:
            await self.session().delete(record)

        await self._flush(change)

    async def _load(
        self,
        resource_type: ResourceType,
        resource_id: str | None,
        parameters: ResponseParameters[Any] | None = None,
    ) -> Any:
        """Load a record, with the collections the response keeps."""
        mapping = self._mapping(resource_type)
        statement = _load_statement(mapping, resource_id, parameters)
        record = None if statement is None else await self.session().scalar(statement)
        if record is None:
            raise self._not_found(resource_type, resource_id)
        return record

    async def _check_uniqueness(
        self, mapping: ResourceMapping, resource: Resource[Any]
    ) -> None:
        session = self.session()
        comparator = await self._ready_comparator(session)
        for statement in self._unique_statements(mapping, resource, comparator):
            if await session.scalar(statement) is not None:
                raise UniquenessException

    async def _ready_comparator(self, session: AsyncSession) -> _Comparator:
        """Return how the database of the session compares strings, ready to compare them."""
        connection = await session.connection()
        comparator = self._comparator(connection.dialect.name)
        raw = await connection.get_raw_connection()
        await comparator.prepare_async(raw.driver_connection)
        return comparator

    async def _links(
        self, mapping: ResourceMapping, resource: Resource[Any]
    ) -> dict[_Collection, list[Any]]:
        links = {}
        for collection, ids in _link_ids(mapping, resource).items():
            statement = _links_statement(collection, ids)
            records = list((await self.session().scalars(statement)).all())
            self._check_links(collection, ids, records)
            links[collection] = records
        return links

    async def _flush(
        self,
        change: Callable[[], Awaitable[None]],
        check_uniqueness: Callable[[], Awaitable[None]] = _nothing_async,
    ) -> None:
        """Apply a change in a savepoint, and flush it, as the synchronous storage does."""
        session = self.session()
        try:
            async with session.begin_nested():
                await change()
                await session.flush()
        except StaleDataError as exception:
            raise PreconditionFailedException from exception
        except IntegrityError:
            await check_uniqueness()
            raise
