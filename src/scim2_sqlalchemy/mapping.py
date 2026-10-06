from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from inspect import isclass
from typing import Any

from pydantic import BaseModel
from scim2_models import Attribute as SchemaAttribute
from scim2_models import AttributeBinding
from scim2_models import Mutability
from scim2_models import Path
from scim2_models import Resource
from scim2_models import Schema
from scim2_models import Uniqueness
from sqlalchemy import ColumnElement
from sqlalchemy import String
from sqlalchemy import cast
from sqlalchemy import inspect
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm import Mapper
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy.orm import strategy_options


class _Storage(Enum):
    """How a mapping stores an attribute.

    A derived attribute is read from another resource, such as the display of
    a member, so writing it changes nothing. A ref is the $ref of a link,
    built from its value: clients may send it, and writing it changes nothing
    either.
    """

    written = "written"
    read_only = "read only"
    derived = "derived"
    ref = "ref"


def _holds_text(expression: Any) -> bool:
    try:
        return expression.type.python_type is str
    except NotImplementedError:
        return True


class Attribute:
    """A column holding a SCIM attribute that the storage only reads, or only writes.

    To store a value in another form, map the attribute to a
    :class:`~sqlalchemy.ext.hybrid.hybrid_property` that converts it, in Python
    and in SQL.

    :param expression: The column, or the
        :class:`~sqlalchemy.ext.hybrid.hybrid_property`, holding the value.
    :param readable: Whether the storage reads the attribute. When
        :data:`False`, the attribute is write-only, such as a password: the
        storage never returns it, nor filters or sorts on it.
    :param writable: Whether the storage writes the attribute. When
        :data:`False`, the attribute is read-only.
    """

    def __init__(
        self, expression: Any, *, readable: bool = True, writable: bool = True
    ) -> None:
        self.expression = expression
        self.readable = readable
        self.writable = writable


class Many:
    """The entries of a multi-valued complex attribute, stored in a child table.

    The entries belong to the resource: writing the resource replaces them.
    The relationship needs ``cascade="all, delete-orphan"``
    (:ref:`sqlalchemy:cascade_delete_orphan`). Its ``order_by`` gives the
    order of the entries. A sort uses the ``primary`` entry, or else
    the first one.

    :param relationship: The :func:`~sqlalchemy.orm.relationship` from the
        resource record to the entry records.
    :param attributes: The columns of the entry records, by sub-attribute name,
        such as ``"value"`` or ``"type"``.
    """

    def __init__(self, relationship: Any, attributes: Mapping[str, Any]) -> None:
        self.relationship = relationship
        self.attributes = attributes


class Link:
    """A complex attribute linking to other resources.

    A multi-valued attribute, such as the ``members`` of a group, maps to a
    relationship holding several records. A single-valued one, such as the
    ``manager`` of an enterprise user, maps to a relationship holding one
    record.

    ``value`` is the ``id`` of the linked resource. Its display name, the
    ``display`` or ``displayName`` sub-attribute, is the ``displayName`` of the
    linked resource when its mapping has one, or else its ``userName``.
    Writing the resource changes the links, and never creates nor deletes the
    linked resources.

    :param relationship: The :func:`~sqlalchemy.orm.relationship` from the
        resource record to the records of the linked resources.
    :param resource_type: The name of the resource type of the linked
        resources, as the storage knows it.
    """

    def __init__(self, relationship: Any, resource_type: str) -> None:
        self.relationship = relationship
        self.resource_type = resource_type


@dataclass(eq=False)
class _Column:
    """A SCIM attribute stored in a column or a hybrid property.

    textual tells whether filters compare the column as text, as they do for
    an integer identifier.
    """

    binding: AttributeBinding
    expression: ColumnElement[Any]
    key: str
    readable: bool
    writable: bool
    textual: bool = False

    @property
    def is_string(self) -> bool:
        target = self.binding.target_type
        return (
            target is not None
            and SchemaAttribute.Type.from_python(target) == SchemaAttribute.Type.string
        )

    @property
    def casefolded(self) -> bool:
        """Whether the values compare without their case."""
        return self.is_string and not self.binding.case_exact

    @property
    def nullable(self) -> bool:
        """Whether the column can hold no value. A hybrid property is taken as nullable."""
        columns = getattr(getattr(self.expression, "property", None), "columns", None)
        return columns is None or bool(columns[0].nullable)

    @property
    def compared(self) -> ColumnElement[Any]:
        """The expression filters compare."""
        if self.textual:
            return cast(self.expression, String)
        return self.expression


@dataclass(eq=False)
class _Collection:
    """A complex attribute stored in related records.

    The entries of an owned collection belong to the resource. The others are
    links to the resources of another mapping, the target. A single collection
    holds one entry at most, such as the manager of a user.
    """

    binding: AttributeBinding
    relationship: InstrumentedAttribute[Any]
    columns: dict[str, _Column] = field(default_factory=dict)
    resource_type: str | None = None
    target: "ResourceMapping | None" = None

    @property
    def owned(self) -> bool:
        return self.resource_type is None

    @property
    def entry_class(self) -> type[Any]:
        return self.relationship_property.mapper.class_

    @property
    def relationship_property(self) -> RelationshipProperty[Any]:
        return self.relationship.property  # type: ignore[return-value]

    @property
    def single(self) -> bool:
        return not self.binding.is_multivalued

    def exists(self, condition: Any = None) -> ColumnElement[bool]:
        """Return the condition that an entry exists, and matches condition when given."""
        method = self.relationship.has if self.single else self.relationship.any
        return method() if condition is None else method(condition)

    @property
    def writable(self) -> bool:
        """Whether writing the resource writes these entries."""
        mutability = self.binding.model.get_field_annotation(
            self.binding.field_name, Mutability
        )
        return bool(mutability != Mutability.read_only)


class ResourceMapping:
    """How the resources of a SCIM model are stored in the records of a SQLAlchemy model.

    The paths are checked against the SCIM model when the mapping is created,
    so a mistake raises at import time rather than at the first request.

    :param model: The SCIM model of the resources, such as
        ``User[EnterpriseUser]``.
    :param record: The SQLAlchemy model of the records.
    :param attributes: The attributes, by SCIM path. A value is a column, a
        :class:`~sqlalchemy.ext.hybrid.hybrid_property`, an :class:`Attribute`, a :class:`Many` or a
        :class:`Link`. ``id``, ``meta.created`` and ``meta.lastModified``
        are required.
    :param version: The ``version_id_col`` of the mapper of ``record``, which
        gives ``meta.version`` (:ref:`sqlalchemy:mapper_version_counter`).
    :raises ValueError: When a path is not valid for the model, or when a
        value does not fit the attribute of its path.
    """

    _REQUIRED = ("id", "meta.created", "meta.lastModified")

    def __init__(
        self,
        model: type[Resource[Any]],
        record: type[Any],
        attributes: Mapping[str, Any],
        *,
        version: Any,
    ) -> None:
        self.model = model
        self.record = record
        self._columns: dict[tuple[type[BaseModel], str, str | None], _Column] = {}
        self._collections: dict[tuple[type[BaseModel], str], _Collection] = {}

        for required in self._REQUIRED:
            if required not in attributes:
                raise ValueError(f"The mapping of {model.__name__} needs {required!r}")
        for path, value in attributes.items():
            self._add(path, value)

        mapper: Mapper[Any] = inspect(record)
        columns = getattr(getattr(version, "property", None), "columns", [None])
        if mapper.version_id_col is None or mapper.version_id_col is not columns[0]:
            raise ValueError(
                f"The version of {model.__name__} must be the version_id_col "
                f"of the mapper of {record.__name__}"
            )
        self._version: InstrumentedAttribute[Any] = version
        self._id = self._column("id")
        self._id.textual = not _holds_text(self._id.expression)
        self._created = self._column("meta.created")
        self._last_modified = self._column("meta.lastModified")

    def schemas(self) -> list[Schema]:
        """Return the schemas of the resources, restricted to what the mapping stores.

        They hold the mapped attributes only, and an attribute the mapping
        reads but never writes is ``readOnly``. Publish them, rather than the
        schemas of :attr:`model`, so that clients know what the server stores:
        build the provider of the server with
        :meth:`ScimProvider.from_discovery <scim2_models.ScimProvider.from_discovery>`,
        and pass it to the storage.

        :returns: The schema of the resource, then the schemas of its extensions.
        """
        models: list[Any] = [self.model, *self.model.get_extension_models().values()]
        return [self._restrict(model.to_schema()) for model in models]

    def _restrict(self, schema: Schema) -> Schema:
        attributes = []
        for attribute in schema.attributes or []:
            restricted = self._restrict_attribute(
                f"{schema.id}:{attribute.name}", attribute
            )
            if restricted is not None:
                attributes.append(restricted)
        return schema.model_copy(update={"attributes": attributes})

    def _restrict_attribute(self, urn: str, attribute: Any) -> Any:
        """Return an attribute of a schema as the mapping stores it, or None when it stores nothing of it."""
        if attribute.sub_attributes:
            sub_attributes = [
                restricted
                for sub_attribute in attribute.sub_attributes
                if (
                    restricted := self._restrict_attribute(
                        f"{urn}.{sub_attribute.name}", sub_attribute
                    )
                )
                is not None
            ]
            if not sub_attributes:
                return None
            written = any(
                sub_attribute.mutability != Mutability.read_only
                for sub_attribute in sub_attributes
            )
            return attribute.model_copy(
                update={
                    "sub_attributes": sub_attributes,
                    "mutability": attribute.mutability
                    if written
                    else Mutability.read_only,
                }
            )

        storage = self._storage(urn)
        if storage is None:
            return None
        if storage == _Storage.written:
            return attribute
        if storage == _Storage.ref:
            linked = self._linked_resource_type(urn)
            return attribute.model_copy(update={"reference_types": [linked]})
        return attribute.model_copy(update={"mutability": Mutability.read_only})

    def _storage(self, urn: str) -> _Storage | None:
        """Return how the mapping stores the attribute of a URN, or None when it does not store it."""
        key = urn.lower()
        for column in self._columns.values():
            if column.binding.urn.lower() == key:
                return _Storage.written if column.writable else _Storage.read_only
        for collection in self._collections.values():
            if not collection.owned:
                head = collection.binding.urn.lower()
                if key == f"{head}.value":
                    return _Storage.written
                if key in (f"{head}.display", f"{head}.displayname"):
                    return _Storage.derived
                if key == f"{head}.$ref":
                    return _Storage.ref
                continue
            for column in collection.columns.values():
                if column.binding.urn.lower() == key:
                    return _Storage.written if column.writable else _Storage.read_only
        return None

    def _nullable(self, urn: str) -> bool:
        """Tell whether the column of an attribute can hold no value, True for an attribute without column."""
        key = urn.lower()
        return all(
            column.nullable
            for column in self._columns.values()
            if column.binding.urn.lower() == key
        )

    def _linked_resource_type(self, urn: str) -> str | None:
        """Return the resource type a Link links to, from the URN of its $ref."""
        head = urn.lower().removesuffix(".$ref")
        return next(
            collection.resource_type
            for collection in self._collections.values()
            if collection.binding.urn.lower() == head
        )

    def _column(self, path: str) -> _Column:
        binding = self._resolve(path)
        return self._columns[
            (binding.model, binding.field_name, binding.sub_field_name)
        ]

    def _lookup(
        self, binding: AttributeBinding
    ) -> tuple[_Collection | None, _Column | None]:
        """Return the collection holding an attribute, if any, and the column of the attribute.

        The column is None for an attribute stored nowhere, and for a
        collection named alone, such as emails.
        """
        collection = self._collections.get((binding.model, binding.field_name))
        if collection is not None:
            if binding.sub_field_name is None:
                return collection, None
            return collection, collection.columns.get(binding.sub_field_name)
        key = (binding.model, binding.field_name, binding.sub_field_name)
        return None, self._columns.get(key)

    def _sub_columns(self, binding: AttributeBinding) -> list[_Column]:
        """Return the mapped sub-attributes of a complex attribute holding a single value."""
        return [
            column
            for (model, field_name, sub_field_name), column in self._columns.items()
            if model is binding.model
            and field_name == binding.field_name
            and sub_field_name is not None
        ]

    def _unique_columns(self) -> Iterator[_Column]:
        """Yield the readable columns whose values no two resources share."""
        for column in self._columns.values():
            if column.readable and column.binding.get_annotation(Uniqueness) in (
                Uniqueness.server,
                Uniqueness.global_,
            ):
                yield column

    def _loader_options(self) -> list[strategy_options._AbstractLoad]:
        """Return the options loading every collection along with the records.

        Loading them up front is what lets an asynchronous session read them.
        """
        return [
            strategy_options.selectinload(collection.relationship)
            for collection in self._collections.values()
        ]

    def _bind(self, mappings: Mapping[str, "ResourceMapping"]) -> None:
        """Bind each Link to the mapping of the resources it links to.

        Several storages may share a mapping, as long as they link it to the
        same mappings.
        """
        for collection in self._collections.values():
            if collection.owned:
                continue
            target = mappings.get(collection.resource_type)  # type: ignore[arg-type]
            if target is None:
                raise ValueError(
                    f"No mapping for the resource type {collection.resource_type!r}"
                )
            if collection.target is not None and collection.target is not target:
                raise ValueError(
                    f"{collection.binding.urn!r} already links to another mapping "
                    f"of {collection.resource_type!r}: a mapping serves storages "
                    "sharing the same mappings"
                )
            collection.target = target
            collection.columns = {}
            head = collection.binding.urn
            for binding, column in (
                (self._resolve(f"{head}.value"), target._id),
                (
                    self._find(f"{head}.display") or self._find(f"{head}.displayName"),
                    target._display(),
                ),
            ):
                if binding is None or column is None:
                    continue
                collection.columns[binding.sub_field_name] = _Column(  # type: ignore[index]
                    binding,
                    column.expression,
                    column.key,
                    column.readable,
                    False,
                    column.textual,
                )

    def _display(self) -> _Column | None:
        """Return the column a link to these resources displays."""
        for path in ("displayName", "userName"):
            binding = self._find(path)
            if binding is None:
                continue
            _, column = self._lookup(binding)
            if column is not None and column.readable:
                return column
        return None

    def _find(self, path: str) -> AttributeBinding | None:
        return Path[self.model](path).resolve()  # type: ignore[name-defined]

    def _resolve(self, path: str) -> AttributeBinding:
        binding = self._find(path)
        if binding is None:
            raise ValueError(f"{path!r} is not an attribute of {self.model.__name__}")
        return binding

    def _add(self, path: str, value: Any) -> None:
        binding = self._resolve(path)
        target = binding.target_type
        is_complex = isclass(target) and issubclass(target, BaseModel)

        if isinstance(value, Many | Link):
            if not (is_complex and binding.sub_field_name is None) or (
                isinstance(value, Many) and not binding.is_multivalued
            ):
                kind = (
                    "a multi-valued complex" if isinstance(value, Many) else "a complex"
                )
                raise ValueError(
                    f"{path!r} is not {kind} attribute of {self.model.__name__}"
                )
            collection = _Collection(binding, value.relationship)
            if collection.relationship_property.uselist == collection.single:
                raise ValueError(
                    f"{path!r} holds {'one entry' if collection.single else 'several entries'}, "
                    f"but its relationship holds {'several' if collection.single else 'one'}"
                )
            if isinstance(value, Many):
                if not collection.relationship_property.cascade.delete_orphan:
                    raise ValueError(
                        f"The relationship of {path!r} needs the delete-orphan cascade"
                    )
                for sub_path, expression in value.attributes.items():
                    sub_binding = self._resolve(f"{path}.{sub_path}")
                    collection.columns[sub_binding.sub_field_name] = (  # type: ignore[index]
                        self._build_column(
                            f"{path}.{sub_path}", sub_binding, expression
                        )
                    )
            else:
                collection.resource_type = value.resource_type
            self._collections[(binding.model, binding.field_name)] = collection
            return

        if binding.is_multivalued or is_complex:
            raise ValueError(
                f"{path!r} holds several values or sub-attributes: map it with Many "
                "or Link, or map its sub-attributes"
            )
        self._columns[(binding.model, binding.field_name, binding.sub_field_name)] = (
            self._build_column(path, binding, value)
        )

    def _build_column(
        self, path: str, binding: AttributeBinding, value: Any
    ) -> _Column:
        """Build the column of an attribute, and decide whether it is written.

        A read-only attribute, or a hybrid property without a setter, is never
        written.
        """
        attribute = value if isinstance(value, Attribute) else Attribute(value)
        expression = attribute.expression
        key = getattr(expression, "key", None)
        if not isinstance(key, str):
            raise ValueError(
                f"{path!r} must be mapped to a column or a hybrid property"
            )

        writable = attribute.writable
        descriptor = inspect(expression.class_).all_orm_descriptors[key]
        mutabilities = {
            binding.model.get_field_annotation(binding.field_name, Mutability),
            binding.get_annotation(Mutability),
        }
        if Mutability.read_only in mutabilities or (
            isinstance(descriptor, hybrid_property) and descriptor.fset is None
        ):
            writable = False
        return _Column(binding, expression, key, attribute.readable, writable)
