import datetime
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import asynccontextmanager
from contextlib import contextmanager
from typing import TYPE_CHECKING
from typing import Any

from scim2_models import ConflictException
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
from scim2_server.keyset import Bound
from scim2_server.keyset import Position
from scim2_server.keyset import keyset_page
from scim2_server.keyset import page_rows
from scim2_server.storage import AsyncScimStorage
from scim2_server.storage import ScimStorage
from scim2_server.storage import SearchPage
from sqlalchemy import Dialect
from sqlalchemy import Select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from .comparison import _Comparator
from .comparison import _comparator
from .conversion import _check_storable
from .conversion import _comparable
from .conversion import _from_scim
from .conversion import _link_entries
from .conversion import _to_scim
from .conversion import _version
from .mapping import ResourceMapping
from .mapping import _Collection
from .mapping import _Column
from .query import _as_key
from .query import _links_statement
from .query import _load_statement
from .query import _records_statement
from .query import _root_statements
from .query import _search_statements
from .query import _taken_statement

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _utcnow() -> datetime.datetime:
    """Return the current time, in UTC."""
    return datetime.datetime.now(datetime.UTC)


def _expire(
    session: "Session | AsyncSession", mapping: ResourceMapping, resource_id: str | None
) -> None:
    """Expire the record of an identifier when the session holds it, so that loading it reads the database again.

    Only this record is read again: a record linking to itself, such as a
    group holding itself, is then loaded once, with all its collections.
    """
    key = _as_key(mapping._id, resource_id)
    if key is None:
        return
    record = session.identity_map.get(session.identity_key(mapping.record, key))
    if record is not None:
        session.expire(record)


def _holds_id(collection: _Collection, record: Any, value: str) -> bool:
    """Tell whether a record a Link links to has an identifier."""
    target_id = collection.target._id  # type: ignore[union-attr]
    return bool(getattr(record, target_id.key) == _as_key(target_id, value))


def _has_text_id(id_column: _Column, record: Any, value: str | None) -> bool:
    """Tell whether a record found by a string identifier has exactly this identifier.

    A collation of the database, such as the default one of MariaDB, can
    ignore the case and the trailing spaces of a string.
    """
    key = _as_key(id_column, value)
    return not isinstance(key, str) or getattr(record, id_column.key) == key


def _load_position(
    position: Any, search_request: SearchRequest[Any], type_count: int
) -> Position | None:
    """Read the position of a cursor page, or return None for the first page.

    At the root, the position holds the index of a resource type.
    """
    return Position.load(
        position,
        has_sort=search_request.sort_by is not None,
        type_count=type_count if type_count > 1 else None,
    )


def _by_position(rows: Sequence[Any]) -> dict[int, list[str]]:
    """Group the identifiers of a page at the root by position of their resource type."""
    ids: dict[int, list[str]] = {}
    for position, record_id, _ in rows:
        ids.setdefault(position, []).append(record_id)
    return ids


_UNIQUE_VIOLATIONS = {
    "23505",
    "SQLITE_CONSTRAINT_UNIQUE",
    "SQLITE_CONSTRAINT_PRIMARYKEY",
    1062,
}
"""The codes of a unique value already taken: the SQLSTATE of PostgreSQL, the
name of the SQLite error, and the number of the MariaDB error."""

_FOREIGN_KEY_VIOLATIONS = {
    "23503",
    "SQLITE_CONSTRAINT_FOREIGNKEY",
    1216,
    1217,
    1451,
    1452,
}
"""The codes of a foreign key the change breaks, such as a record still
referring to a deleted one, as _UNIQUE_VIOLATIONS gives them."""

_CHANGED_MEANWHILE = 1020
"""The number of the MariaDB error on a record another transaction changed since it was read."""

_SAVEPOINT_GONE = 1305
"""The number of the MariaDB error on a savepoint that no longer exists."""


def _error_code(error: BaseException | None) -> Any:
    """Return the code of an error of a database driver.

    The MariaDB drivers give the number of the error, in errno or first in
    args, and a SQLSTATE too generic to tell a unique value taken.
    """
    errno = getattr(error, "errno", None)
    if isinstance(errno, int):
        return errno
    args = getattr(error, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    for attribute in ("sqlstate", "pgcode", "sqlite_errorname"):
        code = getattr(error, attribute, None)
        if code is not None:
            return code
    return None


def _translate(exception: DBAPIError) -> None:
    """Raise the SCIM error of a database error: 409 for a unique value taken or a foreign key broken, 412 for a record changed meanwhile.

    MariaDB rolls back the whole transaction on a record changed meanwhile.
    Rolling back to the savepoint then fails, and its error holds the first
    one as its context.
    """
    code = _error_code(exception.orig)
    if code == _SAVEPOINT_GONE and isinstance(exception.__context__, DBAPIError):
        code = _error_code(exception.__context__.orig)
    if code in _UNIQUE_VIOLATIONS:
        raise UniquenessException from exception
    if code in _FOREIGN_KEY_VIOLATIONS:
        raise ConflictException(
            detail="Another record refers to this resource, or it refers to a missing one"
        ) from exception
    if code == _CHANGED_MEANWHILE:
        raise PreconditionFailedException from exception


class _StorageBase:
    """What the synchronous and asynchronous storages share: everything but the queries."""

    def __init__(
        self,
        mappings: Mapping[str, ResourceMapping],
        provider: ScimProvider | None,
    ) -> None:
        self.mappings = dict(mappings)
        self.provider = provider
        self._policy = provider.policy if provider else ScimPolicy()
        self._comparators: dict[str, _Comparator] = {}
        self._endpoints = {
            str(resource_type.name): str(resource_type.endpoint).strip("/")
            for resource_type in (provider.resource_types if provider else ())
        }
        for resource_type in provider.resource_types if provider else ():
            if resource_type.name not in self.mappings:
                raise ValueError(
                    f"No mapping for the resource type {resource_type.name!r}"
                )
        links = [mapping._targets(self.mappings) for mapping in self.mappings.values()]
        for mapping, targets in zip(self.mappings.values(), links, strict=True):
            mapping._bind(targets)

    def _mapping(self, resource_type: ResourceType) -> ResourceMapping:
        """Return the mapping of a resource type."""
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
            for position, record_id, _ in rows
            if (position, record_id) in records
        ]

    def _root_search_page(
        self,
        resource_types: list[ResourceType],
        total: int,
        rows: list[Any],
        more: bool,
        records: dict[tuple[int, str], Any],
        search_request: SearchRequest[Any],
        anchor: Position | None,
    ) -> SearchPage:
        """Build the page of a search at the root, from its rows and the records they load."""
        resources = self._root_page(resource_types, rows, records, search_request)
        if search_request.cursor is None:
            return SearchPage(total, resources)
        if not rows:
            return keyset_page(total, resources, None, None, more, anchor)
        first, last = (
            Bound(key, record_id, type_position)
            for type_position, record_id, key in (rows[0], rows[-1])
        )
        return keyset_page(total, resources, first, last, more, anchor)

    def _page(
        self,
        resource_type: ResourceType,
        total: int,
        rows: Sequence[Any],
        search_request: SearchRequest[Any],
        anchor: Position | None,
    ) -> SearchPage:
        """Build the page of a search on one resource type, from its records with their sort value and identifier."""
        page, more = page_rows(rows, search_request, anchor)
        resources = [
            self._to_scim(resource_type, record, search_request)
            for record, _, _ in page
        ]
        if search_request.cursor is None:
            return SearchPage(total, resources)
        if not page:
            return keyset_page(total, resources, None, None, more, anchor)
        first, last = (
            Bound(key, record_id) for _, key, record_id in (page[0], page[-1])
        )
        return keyset_page(total, resources, first, last, more, anchor)

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
        self,
        resource_type: ResourceType,
        resource: Resource[Any],
        stored: Resource[Any] | None,
    ) -> None:
        """Refuse a change the mapping cannot store. stored is the stored resource, None for a new one."""
        _check_storable(self._mapping(resource_type), resource, stored)

    @staticmethod
    def _not_found(
        resource_type: ResourceType, resource_id: str | None
    ) -> NotFoundException:
        """Return the error of a resource that does not exist."""
        return NotFoundException(
            detail=f"{resource_type.name} {resource_id!r} not found"
        )

    @staticmethod
    def _check_version(
        mapping: ResourceMapping, record: Any, expected_version: str | None
    ) -> None:
        """Refuse to change a record whose version is not the expected one."""
        if (
            expected_version is not None
            and _version(mapping, record) != expected_version
        ):
            raise PreconditionFailedException

    def _comparator(self, dialect: Dialect) -> _Comparator:
        """Return how a database compares strings, built once for each dialect."""
        if dialect.name not in self._comparators:
            self._comparators[dialect.name] = _comparator(
                dialect, self._policy, self.mappings.values()
            )
        return self._comparators[dialect.name]

    @staticmethod
    def _unique_statements(
        mapping: ResourceMapping,
        resource: Resource[Any],
        comparator: _Comparator,
        stored: Resource[Any] | None,
    ) -> Iterator[Select[Any]]:
        """Yield the statements finding another record holding a unique value of the resource.

        A missing value never clashes, as a SQL NULL does not, and a value the
        storage never writes, such as ``id``, is not stored. stored is the
        resource an update changes, and None for a new one. A value it already
        holds is not checked: another application may have stored a value that
        clashes with it, and the record must remain writable.
        """
        resource_id = None if stored is None else resource.id
        for column in mapping._unique_columns():
            if not column.writable:
                continue
            path = Path(column.binding.urn)
            value = path.get(resource, strict=False)
            if value is None or (
                stored is not None
                and _comparable(value) == _comparable(path.get(stored, strict=False))
            ):
                continue
            yield _taken_statement(mapping, column, value, resource_id, comparator)

    def _assign_links(
        self, found: list[tuple[_Collection, list[Any]]], entries: list[Any]
    ) -> dict[_Collection, list[Any]]:
        """Give each entry of an attribute the record it links to, in the collection of the first Link holding that record.

        found holds the records each Link of the attribute holds among the
        values of the entries. An entry with a type links to a Link giving
        the same type.
        """
        links: dict[_Collection, list[Any]] = {
            collection: [] for collection, _ in found
        }
        urn = found[0][0].binding.urn
        for entry in entries:
            matches = [
                (collection, record)
                for collection, records in found
                for record in records
                if _holds_id(collection, record, entry.value)
            ]
            if not matches:
                raise InvalidValueException(
                    detail=f"'{urn}' links to an unknown resource"
                )
            entry_type = getattr(entry, "type", None)
            if entry_type is not None:
                matches = [
                    (collection, record)
                    for collection, record in matches
                    if self._same_type(collection, entry_type)
                ]
                if not matches:
                    raise InvalidValueException(
                        detail=f"'{urn}' links to {entry.value!r}, which is not "
                        f"of type {entry_type!r}"
                    )
            collection, record = matches[0]
            if record not in links[collection]:
                links[collection].append(record)
        return links

    def _same_type(self, collection: _Collection, value: str) -> bool:
        """Tell whether a type is the one a Link gives, compared as filters compare it."""
        binding = collection.type_column.binding  # type: ignore[union-attr]
        try:
            return bool(
                binding.comparable(value, self._policy)
                == binding.comparable(collection.link_type, self._policy)
            )
        except ValueError:
            return False

    def _new_record(self, resource_type: ResourceType, resource: Resource[Any]) -> Any:
        """Build an empty record for a new resource, with its dates.

        The resource is written once the record is in the session, so that the
        records it links to do not pull a record the session does not know.
        """
        mapping = self._mapping(resource_type)
        record = mapping.record()
        now = _utcnow()
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
        """Write a resource in its record, and date the write.

        The date changes the row even when only links change, such as the
        members of a group, so that the version changes too.
        """
        _from_scim(mapping, resource, record, links, creating=False)
        setattr(record, mapping._last_modified.key, _utcnow())


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
    :param provider: The provider of the server. The storage then returns
        instances of its models, rather than of the models of the mappings,
        and gives each link a ``$ref`` relative to the SCIM root, from the
        endpoints of its resource types. Strings are compared with the
        :attr:`~scim2_models.ScimPolicy.comparison_key` of its policy.
        Each of its resource types needs a mapping.
    :raises ValueError: When a resource type has no mapping.
    """

    supports_cursors = True

    def __init__(
        self,
        mappings: Mapping[str, ResourceMapping],
        session: Callable[[], Session],
        provider: ScimProvider | None = None,
    ) -> None:
        super().__init__(mappings, provider)
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
        """Return a resource, with the attributes the response keeps."""
        record = self._load(resource_type, resource_id, response_parameters)
        return self._to_scim(resource_type, record, response_parameters)

    def search(
        self,
        resource_types: list[ResourceType],
        search_request: SearchRequest[Any],
        *,
        position: Any = None,
    ) -> SearchPage:
        """Return a page of the resources matching a search, on one resource type or at the root."""
        if not resource_types:
            return SearchPage(0, [])
        session = self.session()
        comparator = self._ready_comparator(session)
        mappings = [self._mapping(resource_type) for resource_type in resource_types]
        anchor = _load_position(position, search_request, len(mappings))
        if len(mappings) == 1:
            count, statement = _search_statements(
                mappings[0], search_request, comparator, anchor
            )
            total = session.scalar(count) or 0
            rows = session.execute(statement).all()
            return self._page(resource_types[0], total, rows, search_request, anchor)

        count, statement = _root_statements(
            mappings, search_request, comparator, anchor
        )
        total = session.scalar(count) or 0
        rows, more = page_rows(session.execute(statement).all(), search_request, anchor)
        records = {}
        for type_position, ids in _by_position(rows).items():
            loading = _records_statement(mappings[type_position], ids, search_request)
            for record_id, record in session.execute(loading):
                records[type_position, record_id] = record
        return self._root_search_page(
            resource_types, total, rows, more, records, search_request, anchor
        )

    def create(
        self, resource_type: ResourceType, resource: Resource[Any]
    ) -> Resource[Any]:
        """Store a new resource, and return it as stored."""
        mapping = self._mapping(resource_type)
        self._check_storable(resource_type, resource, None)
        self._check_uniqueness(mapping, resource, None)
        links = self._links(mapping, resource)
        record = self._new_record(resource_type, resource)

        def change() -> None:
            self.session().add(record)
            _from_scim(mapping, resource, record, links, creating=True)

        self._flush(change)
        return self.get(resource_type, str(getattr(record, mapping._id.key)))

    def update(
        self,
        resource_type: ResourceType,
        resource: Resource[Any],
        *,
        expected_version: str | None = None,
    ) -> Resource[Any]:
        """Replace a resource, and return it as stored."""
        mapping = self._mapping(resource_type)
        record = self._load(resource_type, resource.id)
        self._check_version(mapping, record, expected_version)
        stored = self._to_scim(resource_type, record)
        self._check_storable(resource_type, resource, stored)
        self._check_uniqueness(mapping, resource, stored)
        links = self._links(mapping, resource)
        self._flush(lambda: self._write_record(mapping, resource, record, links))
        return self.get(resource_type, resource.id)  # type: ignore[arg-type]

    def delete(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected_version: str | None = None,
    ) -> None:
        """Delete a resource."""
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
        _expire(self.session(), mapping, resource_id)
        record = None if statement is None else self.session().scalar(statement)
        if record is None or not _has_text_id(mapping._id, record, resource_id):
            raise self._not_found(resource_type, resource_id)
        return record

    def _check_uniqueness(
        self,
        mapping: ResourceMapping,
        resource: Resource[Any],
        stored: Resource[Any] | None,
    ) -> None:
        """Refuse a resource with a unique value another resource already has."""
        session = self.session()
        comparator = self._ready_comparator(session)
        for statement in self._unique_statements(mapping, resource, comparator, stored):
            if session.scalar(statement) is not None:
                raise UniquenessException

    def _ready_comparator(self, session: Session) -> _Comparator:
        """Return how the database of the session compares strings, ready to compare them."""
        connection = session.connection()
        comparator = self._comparator(connection.dialect)
        comparator.prepare(connection.connection.driver_connection)
        return comparator

    def _links(
        self, mapping: ResourceMapping, resource: Resource[Any]
    ) -> dict[_Collection, list[Any]]:
        """Return the records each Link of a resource links to, refusing a value that links to nothing."""
        links: dict[_Collection, list[Any]] = {}
        for collections, entries in _link_entries(mapping, resource):
            ids = [entry.value for entry in entries]
            found = [
                (
                    collection,
                    list(
                        self.session().scalars(_links_statement(collection, ids)).all()
                    ),
                )
                for collection in collections
            ]
            links |= self._assign_links(found, entries)
        return links

    def _flush(self, change: Callable[[], None]) -> None:
        """Apply a change in a savepoint, and flush it.

        A version changed in the meantime by another transaction raises a 412.
        A unique value taken in the meantime raises a 409, as does a value
        the unique constraint of the database refuses, such as a value that
        differs only by its accents under a collation ignoring them. A
        foreign key the change breaks raises a 409 too, such as a record
        outside SCIM still referring to a deleted resource.
        """
        session = self.session()
        try:
            with session.begin_nested():
                change()
                session.flush()
        except StaleDataError as exception:
            raise PreconditionFailedException from exception
        except DBAPIError as exception:
            _translate(exception)
            raise


class AsyncSqlAlchemyStorage(_StorageBase, AsyncScimStorage):
    """The asynchronous variant of :class:`SqlAlchemyStorage`.

    It needs the ``asyncio`` extra, which installs the asynchronous support of
    SQLAlchemy: ``pip install "scim2-sqlalchemy[asyncio]"``.

    :param mappings: The mapping of each resource type, by resource type name.
    :param session: Return the current
        :class:`~sqlalchemy.ext.asyncio.AsyncSession`, such as an
        :class:`~sqlalchemy.ext.asyncio.async_scoped_session`.
    :param provider: The provider of the server. The storage then returns
        instances of its models, rather than of the models of the mappings.
        Strings are compared with the
        :attr:`~scim2_models.ScimPolicy.comparison_key` of its policy.
        Each of its resource types needs a mapping.
    :raises ValueError: When a resource type has no mapping.
    """

    supports_cursors = True

    def __init__(
        self,
        mappings: Mapping[str, ResourceMapping],
        session: Callable[[], "AsyncSession"],
        provider: ScimProvider | None = None,
    ) -> None:
        super().__init__(mappings, provider)
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
        """Return a resource, with the attributes the response keeps."""
        record = await self._load(resource_type, resource_id, response_parameters)
        return self._to_scim(resource_type, record, response_parameters)

    async def search(
        self,
        resource_types: list[ResourceType],
        search_request: SearchRequest[Any],
        *,
        position: Any = None,
    ) -> SearchPage:
        """Return a page of the resources matching a search, on one resource type or at the root."""
        if not resource_types:
            return SearchPage(0, [])
        session = self.session()
        comparator = await self._ready_comparator(session)
        mappings = [self._mapping(resource_type) for resource_type in resource_types]
        anchor = _load_position(position, search_request, len(mappings))
        if len(mappings) == 1:
            count, statement = _search_statements(
                mappings[0], search_request, comparator, anchor
            )
            total = await session.scalar(count) or 0
            rows = (await session.execute(statement)).all()
            return self._page(resource_types[0], total, rows, search_request, anchor)

        count, statement = _root_statements(
            mappings, search_request, comparator, anchor
        )
        total = await session.scalar(count) or 0
        rows, more = page_rows(
            (await session.execute(statement)).all(), search_request, anchor
        )
        records = {}
        for type_position, ids in _by_position(rows).items():
            loading = _records_statement(mappings[type_position], ids, search_request)
            for record_id, record in await session.execute(loading):
                records[type_position, record_id] = record
        return self._root_search_page(
            resource_types, total, rows, more, records, search_request, anchor
        )

    async def create(
        self, resource_type: ResourceType, resource: Resource[Any]
    ) -> Resource[Any]:
        """Store a new resource, and return it as stored."""
        mapping = self._mapping(resource_type)
        self._check_storable(resource_type, resource, None)
        await self._check_uniqueness(mapping, resource, None)
        links = await self._links(mapping, resource)
        record = self._new_record(resource_type, resource)

        async def change() -> None:
            self.session().add(record)
            _from_scim(mapping, resource, record, links, creating=True)

        await self._flush(change)
        return await self.get(resource_type, str(getattr(record, mapping._id.key)))

    async def update(
        self,
        resource_type: ResourceType,
        resource: Resource[Any],
        *,
        expected_version: str | None = None,
    ) -> Resource[Any]:
        """Replace a resource, and return it as stored."""
        mapping = self._mapping(resource_type)
        record = await self._load(resource_type, resource.id)
        self._check_version(mapping, record, expected_version)
        stored = self._to_scim(resource_type, record)
        self._check_storable(resource_type, resource, stored)
        await self._check_uniqueness(mapping, resource, stored)
        links = await self._links(mapping, resource)

        async def change() -> None:
            self._write_record(mapping, resource, record, links)

        await self._flush(change)
        return await self.get(resource_type, resource.id)  # type: ignore[arg-type]

    async def delete(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected_version: str | None = None,
    ) -> None:
        """Delete a resource."""
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
        _expire(self.session(), mapping, resource_id)
        record = None if statement is None else await self.session().scalar(statement)
        if record is None or not _has_text_id(mapping._id, record, resource_id):
            raise self._not_found(resource_type, resource_id)
        return record

    async def _check_uniqueness(
        self,
        mapping: ResourceMapping,
        resource: Resource[Any],
        stored: Resource[Any] | None,
    ) -> None:
        """Refuse a resource with a unique value another resource already has."""
        session = self.session()
        comparator = await self._ready_comparator(session)
        for statement in self._unique_statements(mapping, resource, comparator, stored):
            if await session.scalar(statement) is not None:
                raise UniquenessException

    async def _ready_comparator(self, session: "AsyncSession") -> _Comparator:
        """Return how the database of the session compares strings, ready to compare them."""
        connection = await session.connection()
        comparator = self._comparator(connection.dialect)
        raw = await connection.get_raw_connection()
        await comparator.prepare_async(raw.driver_connection)
        return comparator

    async def _links(
        self, mapping: ResourceMapping, resource: Resource[Any]
    ) -> dict[_Collection, list[Any]]:
        """Return the records each Link of a resource links to, refusing a value that links to nothing."""
        links: dict[_Collection, list[Any]] = {}
        for collections, entries in _link_entries(mapping, resource):
            ids = [entry.value for entry in entries]
            found = []
            for collection in collections:
                statement = _links_statement(collection, ids)
                records = (await self.session().scalars(statement)).all()
                found.append((collection, list(records)))
            links |= self._assign_links(found, entries)
        return links

    async def _flush(self, change: Callable[[], Awaitable[None]]) -> None:
        """Apply a change in a savepoint, and flush it, as the synchronous storage does."""
        session = self.session()
        try:
            async with session.begin_nested():
                await change()
                await session.flush()
        except StaleDataError as exception:
            raise PreconditionFailedException from exception
        except DBAPIError as exception:
            _translate(exception)
            raise
