import base64
import datetime
import warnings
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from scim2_models import AttributeBinding
from scim2_models import Mutability
from scim2_models import Resource
from scim2_models import Schema
from scim2_server import mapping as core
from scim2_server.mapping import MappingProblem
from scim2_server.mapping.declaration import key
from scim2_server.mapping.declaration import mutabilities
from scim2_server.mapping.declaration import never_returned
from scim2_server.mapping.mapping import Problems
from sqlalchemy import Column
from sqlalchemy import ColumnElement
from sqlalchemy import Dialect
from sqlalchemy import Enum as SqlEnum
from sqlalchemy import String
from sqlalchemy import Uuid
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import MANYTOONE
from sqlalchemy.orm import ONETOMANY
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm import Mapper
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy.orm import strategy_options
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.visitors import iterate
from sqlalchemy.types import TypeDecorator
from sqlalchemy.types import TypeEngine

_MARIADB = ("mysql", "mariadb")
"""The names of the dialects connecting to MariaDB."""

_MARIADB_TEXT = mysql.TEXT(charset="utf8mb4", collation="utf8mb4_nopad_bin")
"""The type of a string column without length on MariaDB, which needs a length for a VARCHAR.

Its values differ by code point, as the storage compares them.
"""

_INTEGERS = range(-(2**63), 2**63)
"""The integers a database can store, in a BIGINT or in an SQLite INTEGER."""

_UUID_GROUPS = ((1, 8), (9, 4), (13, 4), (17, 4), (21, 12))
"""The start and the length of each group of digits of a UUID, without its hyphens."""


class _UuidText(FunctionElement[str]):
    """The text of a UUID column, in the form with hyphens that SCIM holds."""

    type = String()
    inherit_cache = True


@compiles(_UuidText)
def _compile_uuid_text(element: _UuidText, compiler: SQLCompiler, **kw: Any) -> str:
    """Write the text of a UUID.

    A database without a UUID type stores its 32 digits without hyphens, so
    the hyphens are put back. A variant of the column type for the database,
    such as a string, is written as it is.
    """
    (column,) = element.clauses
    text: ColumnElement[str] = cast(column, String)
    uuid_type = _uuid_type(column.type.dialect_impl(compiler.dialect))
    if uuid_type is not None and not (
        compiler.dialect.supports_native_uuid and uuid_type.native
    ):
        groups = [
            func.substr(text, start, length, type_=String)
            for start, length in _UUID_GROUPS
        ]
        text = groups[0]
        for group in groups[1:]:
            text = text + "-" + group
    return compiler.process(text, **kw)


def _uuid_type(sql_type: Any) -> Uuid[Any] | None:
    """Return the Uuid type of a column, also under a TypeDecorator, or None for another type."""
    sql_type = getattr(sql_type, "impl_instance", sql_type)
    return sql_type if isinstance(sql_type, Uuid) else None


def _table_column(expression: Any) -> Column[Any] | None:
    """Return the table column of an expression, or None for a hybrid property or a column property over a SQL expression."""
    columns = getattr(getattr(expression, "property", None), "columns", None)
    column = columns[0] if columns else None
    return column if isinstance(column, Column) else None


def _needs_value(column: Column[Any]) -> bool:
    """Whether a new row needs a value: the column holds no null, has no default, and the database does not number it."""
    return (
        not column.nullable
        and column.default is None
        and column.server_default is None
        and column is not column.table.autoincrement_column
    )


def _python_type(expression: Any) -> type | None:
    """Return the Python type of the values of an expression, or None if its type tells none.

    SQLAlchemy 2.0 raises NotImplementedError for such a type, and 2.1 returns object.
    """
    try:
        python_type: type = expression.type.python_type
    except NotImplementedError:
        return None
    return None if python_type is object else python_type


def _holds_text(expression: Any) -> bool:
    """Whether an expression holds strings. A type that tells no Python type is taken as text."""
    return _python_type(expression) in (str, None)


def _as_scim(value: Any) -> Any:
    """Return a value read from the database in the form SCIM holds it.

    A date without a time zone is taken as UTC, since SQLite drops the zone.
    Binary values are encoded in base64, the form the models decode them from.
    """
    if isinstance(value, datetime.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    return value


def _as_key(column: "_Column", value: Any) -> Any:
    """Return an identifier in the type of its column, or None when it cannot have that type."""
    python_type = _python_type(column.expression)
    if python_type is None:
        return value
    try:
        key = python_type(value)
    except (TypeError, ValueError):
        return None
    if isinstance(key, int) and key not in _INTEGERS:
        return None
    return key


def _writes(descriptor: Any, mapped: Any) -> bool:
    """Tell whether the storage can write a value: a hybrid property needs a setter, and a computed column is never written."""
    if isinstance(descriptor, hybrid_property) and descriptor.fset is None:
        return False
    return getattr(mapped, "computed", None) is None


def _column_name(column: Column[Any]) -> str:
    """Return the name of a column of a table, as the warnings give it."""
    return f"{column.table.name}.{column.name}"


def _sql_writable(binding: AttributeBinding) -> bool:
    """Tell whether the schema lets clients write an attribute."""
    mutability = binding.model.get_field_annotation(binding.field_name, Mutability)
    return bool(mutability != Mutability.read_only)


class Hashed(TypeDecorator[str]):
    """A column type storing the hash of a string, such as a password.

    The column stores what the hash function returns for the value written.
    Map it to an attribute that the schema makes ``writeOnly``, such as
    ``password``, or never returns. The storage reads the hash back, and
    clients never see it. A replacement without the value gives the hash back
    to the storage, which keeps it as it is.

    :param hash_value: The function hashing a value.
    :param length: The length of the column. Without it, MariaDB stores a TEXT.
    """

    impl = String
    cache_ok = True

    def __init__(
        self, hash_value: Callable[[str], str], length: int | None = None
    ) -> None:
        super().__init__(length)
        self.hash_value = hash_value
        self.length = length

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Return the type of the column, a TEXT on MariaDB without a length."""
        if self.length is None and dialect.name in _MARIADB:
            return dialect.type_descriptor(_MARIADB_TEXT)
        return dialect.type_descriptor(self.impl_instance)

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Hash the value before it is sent to the database."""
        return None if value is None else self.hash_value(value)


class Attribute(core.Attribute):
    """A column holding a SCIM attribute, whose writing can be turned off.

    To store a value in another form, map the attribute to a
    :class:`~sqlalchemy.ext.hybrid.hybrid_property` that converts it, in Python
    and in SQL.

    :param expression: The column, or the
        :class:`~sqlalchemy.ext.hybrid.hybrid_property`, holding the value.
    :param writable: Whether the storage writes the attribute. When
        :data:`False`, the attribute is read-only.
    """

    def __init__(self, expression: Any, *, writable: bool = True) -> None:
        super().__init__(expression, writable=writable)

    @property
    def expression(self) -> Any:
        """The column, or the hybrid property, holding the value."""
        return self.target


class Many(core.Many):
    """The entries of a multi-valued attribute, stored in a child table.

    The entries belong to the resource: writing the resource replaces them.
    The relationship needs ``cascade="all, delete-orphan"``
    (:ref:`sqlalchemy:cascade_delete_orphan`). Its ``order_by`` gives the
    order of the entries. Without it, the order is the one the database
    returns. A sort uses the ``primary`` entry, or else the first one.

    Since writing replaces the entries, they cannot hold values that clients
    never read back, such as write-only values. Their columns are also either
    all written or none.

    The entries of a multi-valued simple attribute, such as a list of strings,
    are the values of one column of the child table. The storage keeps their
    order and their duplicates.

    :param relationship: The :func:`~sqlalchemy.orm.relationship` from the
        resource record to the entry records.
    :param attributes: The columns of the entry records, by sub-attribute name,
        such as ``"value"`` or ``"type"``. For a simple attribute, the column
        holding the values.
    """

    def __init__(self, relationship: Any, attributes: Mapping[str, Any] | Any) -> None:
        super().__init__(relationship, attributes)

    @property
    def relationship(self) -> Any:
        """The relationship from the resource record to the entry records."""
        return self.relation


class Entry(core.Entry):
    """A multi-valued complex attribute holding one entry at most, stored in columns of the resource record.

    Such as the email address of a user, in its ``email`` column. The
    resource holds an entry when one of the columns has a value, so the
    columns accept ``NULL``: a write removing the entry empties them. A write
    with several entries is refused.

    :param attributes: The columns of the resource record holding the
        sub-attributes, by sub-attribute name, such as
        ``{"value": UserRecord.email}``. A value may be an :class:`Attribute`.
    :param type: The ``type`` sub-attribute of the entry, such as
        ``"work"``. A client may send it, and a write with another type is
        refused. Without it, the storage does not store ``type``.
    :param primary: The ``primary`` sub-attribute of the entry. A client may
        send any value, as the lone entry is the primary one, and reads this
        one back. Without it, the storage does not store ``primary``.
    """


class Link(core.Link):
    """A complex attribute linking to other resources.

    A multi-valued attribute, such as the ``members`` of a group, maps to a
    relationship holding several records. A single-valued one, such as the
    ``manager`` of an enterprise user, maps to a relationship holding one
    record.

    ``value`` is the ``id`` of the linked resource. Its display name, the
    ``display`` or ``displayName`` sub-attribute, is the ``displayName`` of the
    linked resource when its mapping has one, or else its ``userName``.
    Without either, the links have no display name, although
    :meth:`ResourceMapping.schemas` publishes it. Writing the resource changes the links, and never creates nor deletes the
    linked resources.

    A Link links to the resources of one resource type. An attribute linking
    to several resource types, such as the ``members`` of a group holding users
    and groups, maps to a list of Links, one per resource type. The entries are
    read in the order of the list.

    :param relationship: The :func:`~sqlalchemy.orm.relationship` from the
        resource record to the records of the linked resources. The foreign
        key of a many-to-one relationship accepts ``NULL``, unless the schema
        makes the attribute required, and the extension holding it too. The
        foreign key of a one-to-many relationship always accepts ``NULL``.
    :param resource_type: The name of the resource type of the linked
        resources, as the storage knows it.
    :param type: The ``type`` sub-attribute of the entries, such as ``"User"``
        for the members of a group, or ``"direct"`` for the groups of a user.
        A client may send it, and a write with another value is refused.
        Without it, the storage does not store ``type``.
    """

    def __init__(
        self, relationship: Any, resource_type: str, *, type: str | None = None
    ) -> None:
        super().__init__(relationship, resource_type, type=type)

    @property
    def relationship(self) -> Any:
        """The relationship from the resource record to the records of the linked resources."""
        return self.relation


@dataclass(eq=False)
class _Column(core.Column):
    """A SCIM attribute stored in a column or a hybrid property."""

    expression: ColumnElement[Any] = None  # type: ignore[assignment]

    @property
    def enum_type(self) -> SqlEnum | None:
        """The type of the column when it is an Enum, which stores strings."""
        sql_type = self.expression.type
        return sql_type if isinstance(sql_type, SqlEnum) else None

    @property
    def compared(self) -> ColumnElement[Any]:
        """The expression filters and sorts compare.

        An Enum column is compared as text. PostgreSQL applies no string
        function to its native enumerations, and sorts them in the order of
        their declaration. A UUID is compared in its form with hyphens.
        """
        if (
            self.textual
            or self.enum_type is not None
            or _uuid_type(self.expression.type) is not None
        ):
            return self.text
        return self.expression

    @property
    def text(self) -> ColumnElement[str]:
        """The values as text, such as a UUID in the form with hyphens."""
        if _uuid_type(self.expression.type) is not None:
            return _UuidText(self.expression)
        return cast(self.expression, String)

    @staticmethod
    def stored_string(enumeration: SqlEnum, member: Enum) -> str:
        """Return the string an Enum column stores for a member: its name, or its value with values_callable."""
        strings: dict[Enum, str] = dict(
            zip(type(member), enumeration.enums, strict=False)
        )
        return strings[member]


@dataclass(eq=False)
class _Collection(core.Collection):
    """A multi-valued attribute, or a complex one, stored in related records.

    relation is the relationship from the resource record to the entries.
    """

    @property
    def relationship(self) -> InstrumentedAttribute[Any]:
        """The relationship holding the entries."""
        relationship: InstrumentedAttribute[Any] = self.relation
        return relationship

    @property
    def relationship_property(self) -> RelationshipProperty[Any]:
        """The property of the relationship holding the entries."""
        relationship: RelationshipProperty[Any] = self.relationship.property
        return relationship

    @property
    def entry_class(self) -> type[Any]:
        """The SQLAlchemy model of the entries."""
        return self.relationship_property.mapper.class_

    def exists(self, condition: Any = None) -> ColumnElement[bool]:
        """Return the condition that an entry exists, and matches condition when given."""
        method = self.relationship.has if self.single else self.relationship.any
        exists: ColumnElement[bool] = (
            method() if condition is None else method(condition)
        )
        return exists


class ResourceMapping(core.ResourceMapping):
    """How the resources of a SCIM model are stored in the records of a SQLAlchemy model.

    The paths and the columns are checked when the mapping is created, so a
    mistake raises at import time rather than at the first request.

    :param model: The SCIM model of the resources, such as
        ``User[EnterpriseUser]``.
    :param record: The SQLAlchemy model of the records.
    :param attributes: The attributes, by SCIM path. A value is a column, a
        :class:`~sqlalchemy.ext.hybrid.hybrid_property`, an
        :class:`Attribute`, a :class:`Many`, an :class:`Entry`, a
        :class:`Link`, or a list of :class:`Link`. ``id`` is required.
        ``meta.created`` and ``meta.lastModified`` are filled by the storage:
        without them, the resources have no such date.
    :param version: The ``version_id_col`` of the mapper of ``record``, which
        gives ``meta.version`` (:ref:`sqlalchemy:mapper_version_counter`).
        It only changes when the record changes, not when a linked record
        does: adding a member to a group leaves the version of the user as it
        is. Without it, ``meta.version`` is a hash of the whole resource,
        which the storage computes with its ``version_key``. The storage then
        reads every collection of a resource, even those a response leaves
        out, such as the members of a group.
    :raises ValueError: When a path is not valid for the model, when a
        value does not fit the attribute of its path, or when ``id`` is mapped
        to a column that can hold ``NULL``. Also when clients could neither
        read nor write an attribute, when an attribute clients read, or an
        immutable one, is mapped to a :class:`Hashed` column, or when removing
        a :class:`Link` would write ``NULL`` in a foreign key that refuses it.

    The mapping warns about a column that needs a value but that the storage
    never writes.
    """

    version: Any
    _id: _Column
    _created: _Column | None
    _last_modified: _Column | None

    def __init__(
        self,
        model: type[Resource[Any]],
        record: type[Any],
        attributes: Mapping[str, Any],
        *,
        version: Any = None,
    ) -> None:
        super().__init__(model, record, attributes, version=version)
        problems = self._resolve()
        errors = [problem.message for problem in problems if problem.level == "error"]
        if errors:
            raise ValueError("\n".join(errors))
        for problem in problems:
            warnings.warn(problem.message, stacklevel=2)

    def schemas(self) -> list[Schema]:
        """Return the schemas of the resources, restricted to what the mapping stores.

        They hold the mapped attributes only. An attribute the mapping reads
        but never writes is ``readOnly``. An attribute whose column cannot be
        ``NULL`` and has no default is ``required``, unless clients never read
        it back. A column with a default that cannot be ``NULL`` leaves its
        attribute optional, but its value cannot be removed. Publish them,
        rather than the schemas of :attr:`model`, so that clients know what
        the server stores: build the provider of the server with
        :meth:`ScimProvider.from_discovery <scim2_models.ScimProvider.from_discovery>`,
        and pass it to the storage.

        :returns: The schema of the resource, then the schemas of its extensions.
        """
        return super().schemas()

    def _loader_options(
        self, collections: Iterable[core.Collection]
    ) -> list[strategy_options._AbstractLoad]:
        """Return the options loading collections along with the records.

        Loading them up front is what lets an asynchronous session read them.
        """
        return [
            strategy_options.selectinload(collection.relation)
            for collection in collections
        ]

    def _new_key(self, resource_id: str) -> Any:
        """Return the identifier a new record gets, in the type of its column.

        Raise a ValueError when the storage cannot write the identifier, or when
        its column cannot hold it.
        """
        (value,) = [
            value
            for binding, value in self._declared.values()
            if key(binding) == key(self._id.binding)
        ]
        if not self._value_writable(value):
            raise ValueError(
                f"The storage cannot write the 'id' of {self.model.__name__}"
            )

        record_key = _as_key(self._id, resource_id)
        if record_key is None:
            raise ValueError(
                f"The 'id' of {self.model.__name__} cannot be {resource_id!r}"
            )
        return record_key

    def _is_target(self, value: Any) -> bool:
        return isinstance(getattr(value, "key", None), str) and hasattr(value, "class_")

    def _value_writable(self, value: Any) -> bool:
        expression = value.target if isinstance(value, core.Attribute) else value
        mapper: Mapper[Any] = inspect(expression.class_)
        descriptor = mapper.all_orm_descriptors[expression.key]
        mapped = mapper.columns.get(expression.key)
        return super()._value_writable(value) and _writes(descriptor, mapped)

    def _check_declared(
        self,
        path: str,
        binding: AttributeBinding,
        value: Any,
        holder: core.Many | core.Entry | None,
    ) -> None:
        """Refuse a Hashed column that clients would read, or that would be immutable."""
        expression = value.target if isinstance(value, core.Attribute) else value
        if not isinstance(getattr(expression, "type", None), Hashed):
            return
        concealed = never_returned(binding) or Mutability.write_only in mutabilities(
            binding
        )
        if not concealed:
            raise ValueError(
                f"{path!r} stores a hash, which clients would read: map it to an "
                "attribute that the schema makes writeOnly or never returns"
            )
        if _immutable(binding):
            raise ValueError(
                f"{path!r} is immutable, so a client sending its value again "
                "must match the stored one, which a hash never does"
            )

    def _build_column(
        self,
        path: str,
        binding: AttributeBinding,
        value: Any,
        owner: core.Collection | None,
    ) -> Problems:
        """Build the column of an attribute, and decide whether it is written.

        A read-only attribute, a hybrid property without a setter, or a
        computed column, is never written.
        """
        expression = value.target if isinstance(value, core.Attribute) else value
        writable = Mutability.read_only not in mutabilities(
            binding
        ) and self._value_writable(value)
        column = _Column(
            binding,
            expression.key,
            writable,
            expression=expression,
        )
        is_id = owner is None and key(binding) == key(self._resolve_path("id"))
        python_type = _python_type(expression)
        if (
            column.is_string
            and column.enum_type is None
            and python_type is not None
            and not issubclass(python_type, (str, bytes))
            and not is_id
        ):
            yield MappingProblem(
                "error",
                "not-text",
                f"{path!r} holds strings, but its column holds "
                f"{python_type.__name__} values: map it to a property whose "
                "expression casts the column to a string",
            )
            return None

        table_column = _table_column(expression)
        column.nullable = table_column is None or bool(table_column.nullable)
        column.needs_value = table_column is not None and _needs_value(table_column)
        if column.enum_type is not None:
            column.enumeration = list(column.enum_type.enums)
        if owner is None and table_column is not None:
            column.stores = _column_name(table_column)
        if is_id:
            column.textual = not _holds_text(expression)
            column.keyed = column.textual
        return column

    def _build_collection(
        self,
        path: str,
        binding: AttributeBinding,
        declaration: core.Many | core.Link,
    ) -> Problems:
        """Build the collection of a relationship, holding as many records as the attribute holds entries."""
        collection = _Collection(binding, declaration.relation)
        relationship = collection.relationship_property
        if relationship.uselist == collection.single:
            yield MappingProblem(
                "error",
                "relation-kind",
                f"{path!r} holds {'one entry' if collection.single else 'several entries'}, "
                f"but its relationship holds {'several' if collection.single else 'one'}",
            )
            return None

        if isinstance(declaration, core.Many):
            if not relationship.cascade.delete_orphan:
                yield MappingProblem(
                    "error",
                    "relation-kind",
                    f"The relationship of {path!r} needs the delete-orphan cascade",
                )
                return None
            return collection

        writable = _sql_writable(binding)
        if (
            relationship.direction is ONETOMANY
            and writable
            and any(not column.nullable for column in relationship.remote_side)
        ):
            yield MappingProblem(
                "error",
                "relation-kind",
                f"Removing a link of {path!r} writes NULL in the foreign key "
                "of the linked record, so it must accept NULL",
            )
            return None

        if relationship.direction is MANYTOONE and writable:
            collection.nullable = all(
                column.nullable for column in relationship.local_columns
            )
        return collection

    def _check_record(self) -> Problems:
        """Refuse an identifier column that can hold NULL, and a version that is not the version counter of the mapper."""
        column = _table_column(self._id.expression)
        if column is not None and column.nullable:
            yield MappingProblem(
                "error",
                "id",
                "'id' is mapped to a column that can hold no value, "
                "but each resource needs one",
            )
        if self.version is None:
            return
        mapper: Mapper[Any] = inspect(self.record)
        columns = getattr(getattr(self.version, "property", None), "columns", [None])
        if mapper.version_id_col is None or mapper.version_id_col is not columns[0]:
            yield MappingProblem(
                "error",
                "version",
                f"The version of {self.model.__name__} must be the version_id_col "
                f"of the mapper of {self.record.__name__}",
            )
            return
        if self._last_modified is None and mapper.version_id_generator is False:
            yield MappingProblem(
                "error",
                "version",
                f"The mapping of {self.model.__name__} has no 'meta.lastModified', "
                "so the storage counts the writes in its version, which the "
                "database cannot generate",
            )

    def _fields_needing_value(self) -> Iterable[str]:
        """Return the columns of the record that need a value the storage never writes by itself.

        The setter of a property may write any column its expression reads.
        SQLAlchemy fills the version, the discriminator of the classes, and
        the key each joined table shares with the table it inherits from.
        """
        dates = (self._created, self._last_modified)
        written: list[Any] = []
        mapper: Mapper[Any] = inspect(self.record)
        for mapped in self._columns.values():
            descriptor = mapper.all_orm_descriptors.get(mapped.key)
            if (mapped.writable or mapped in dates) and isinstance(
                descriptor, hybrid_property
            ):
                written += [
                    element.table.columns.get(element.key)
                    for element in iterate(mapped.expression.__clause_element__())  # type: ignore[attr-defined]
                    if isinstance(element, Column)
                ]
        for collection in self._all_collections():
            if collection.single and not collection.owned and collection.writable:
                written += collection.relationship_property.local_columns  # type: ignore[attr-defined]
        written += [mapper.version_id_col, mapper.polymorphic_on]
        for ancestor in mapper.iterate_to_root():
            if ancestor.inherits is not None:
                written += [
                    column
                    for column in ancestor.local_table.primary_key
                    if column.foreign_keys
                ]
        return [
            _column_name(column)
            for column in mapper.columns
            if isinstance(column, Column)
            and _needs_value(column)
            and not any(column is other for other in written)
        ]

    def _links_to(self, collection: core.Collection, target: Any) -> bool:
        return bool(collection.entry_class is target.record)  # type: ignore[attr-defined]

    def _read(self, column: core.Column, record: Any) -> Any:
        """Return the SCIM value of a column of a record."""
        value = getattr(record, column.key)
        if value is None:
            return None
        enum_type = column.enum_type  # type: ignore[attr-defined]
        if isinstance(value, Enum) and enum_type is not None:
            return column.stored_string(enum_type, value)  # type: ignore[attr-defined]
        return _as_scim(value)

    def _related(self, collection: core.Collection, record: Any) -> list[Any]:
        """Return the records a collection of a record holds, as a list even for a single one."""
        related = getattr(record, collection.relation.key)
        if collection.single:
            return [] if related is None else [related]
        return list(related)

    def _counter(self, record: Any) -> Any:
        return getattr(record, self.version.key)

    def _holds_id(self, record: Any, value: str) -> bool:
        return bool(getattr(record, self._id.key) == _as_key(self._id, value))


def _immutable(binding: AttributeBinding) -> bool:
    """Tell whether the schema keeps a value clients read from changing."""
    found = mutabilities(binding)
    return Mutability.immutable in found and Mutability.write_only not in found
