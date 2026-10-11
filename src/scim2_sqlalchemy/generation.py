import hashlib
import re
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scim2_models import Attribute as SchemaAttribute
from scim2_models import AttributeBinding
from scim2_models import Mutability
from scim2_models import Path
from scim2_models import Required
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_models import Uniqueness
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import LargeBinary
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Uuid
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeEngine

from .mapping import Attribute
from .mapping import Link
from .mapping import Many
from .mapping import ResourceMapping

_GROUPS = "urn:ietf:params:scim:schemas:core:2.0:User:groups"
_MEMBERS = "urn:ietf:params:scim:schemas:core:2.0:Group:members"

_MAX_NAME_LENGTH = 63
"""The longest identifier PostgreSQL keeps."""

_SQL_TYPES: dict[SchemaAttribute.Type, Callable[[], TypeEngine[Any]]] = {
    SchemaAttribute.Type.string: String,
    SchemaAttribute.Type.reference: String,
    SchemaAttribute.Type.boolean: Boolean,
    SchemaAttribute.Type.integer: Integer,
    SchemaAttribute.Type.decimal: Float,
    SchemaAttribute.Type.date_time: lambda: DateTime(timezone=True),
    SchemaAttribute.Type.binary: LargeBinary,
}


def _snake(name: str) -> str:
    """Return a name in snake case, such as user_name for userName."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


def _fit(name: str) -> str:
    """Shorten a name longer than PostgreSQL keeps, ending it with a hash of the full name."""
    if len(name) <= _MAX_NAME_LENGTH:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{name[: _MAX_NAME_LENGTH - 9]}_{digest}"


def _sub_attribute(attribute: Any, name: str) -> Any:
    """Return the sub-attribute of an attribute by its name, or None."""
    return next(
        (sub for sub in attribute.sub_attributes or [] if sub.name == name), None
    )


@dataclass
class _Schema:
    """A schema of a resource type, with the prefixes of the paths and of the columns of its attributes."""

    urn: str
    attributes: list[Any]
    path_prefix: str
    column_prefix: str


@dataclass
class _Forward:
    """An attribute linking to resources, with the association table of each multi-valued link."""

    owner: str
    attribute: Any
    targets: list[str]
    keys: dict[str, str]
    tables: dict[str, Table]
    inverse_keys: dict[str, str]


class _Class:
    """The namespace of a record class being built, and the attributes of its mapping."""

    def __init__(self, name: str, table: str) -> None:
        self.name = name
        self.namespace: dict[str, Any] = {"__tablename__": table}
        self.attributes: list[tuple[str, Callable[[], Any]]] = []

    def add(self, key: str, value: Any, path: str) -> None:
        """Add a column or a relationship to the class, and refuse a name already used."""
        if key in self.namespace:
            raise ValueError(
                f"{path!r} needs the name {key!r}, which {self.name!r} already uses"
            )
        self.namespace[key] = value


class _Generator:
    """Build the tables, the record classes and the mappings of the resource types of a provider."""

    def __init__(
        self,
        provider: ScimProvider,
        metadata: MetaData,
        prefix: str,
        store: Callable[[AttributeBinding], bool | TypeEngine[Any]],
        inverses: Mapping[str, str],
    ) -> None:
        self.provider = provider
        self.metadata = metadata
        self.prefix = prefix
        self.store = store
        self.inverses = inverses
        self.decisions: dict[str, bool | TypeEngine[Any]] = {}
        self.resource_types = {str(rt.name): rt for rt in provider.resource_types}
        self.tables = {
            name: _fit(prefix + _snake(rt.endpoint or name))
            for name, rt in self.resource_types.items()
        }
        self.base: Any = type("Base", (DeclarativeBase,), {"metadata": metadata})
        self.classes: dict[str, Any] = {}
        self.schemas = {
            name: self._schemas(rt) for name, rt in self.resource_types.items()
        }
        self.forwards: dict[str, _Forward] = {}
        self.reverses: dict[str, _Forward] = {}

    def _schemas(self, resource_type: ResourceType) -> list[_Schema]:
        """Return the core schema and the extensions of a resource type."""
        model: Any = self.provider.model_for(resource_type)
        schemas = [_Schema(model.__schema__, model.to_schema().attributes, "", "")]
        for urn, extension in model.get_extension_models().items():
            schema = extension.to_schema()
            schemas.append(
                _Schema(
                    urn,
                    schema.attributes,
                    f"{urn}:",
                    f"{_snake(str(schema.name or extension.__name__))}_",
                )
            )
        return schemas

    def generate(self) -> dict[str, ResourceMapping]:
        """Return the mappings of the resource types, by name."""
        self._decide()
        for name in self.resource_types:
            self._find_forwards(name)
        self._find_inverses()
        built = {name: self._build(name) for name in self.resource_types}
        for record_class in built.values():
            self._make(record_class)
        self.base.registry.configure()
        return {
            name: ResourceMapping(
                self.provider.model_for(self.resource_types[name]),  # type: ignore[arg-type]
                self.classes[name],
                {path: build() for path, build in record_class.attributes},
                version=self.classes[name].meta_version,
            )
            for name, record_class in built.items()
        }

    def _decide(self) -> None:
        """Ask store how to keep each attribute the tables could hold, and check the answers."""
        clear = []
        for name, schemas in self.schemas.items():
            model = self.provider.model_for(self.resource_types[name])
            for schema in schemas:
                for attribute in schema.attributes:
                    urn = f"{schema.urn}:{attribute.name}"
                    if self._linked(attribute):
                        if (
                            attribute.mutability != Mutability.read_only
                            or urn in self.inverses
                        ):
                            self._ask(model, urn, column=False)
                        continue
                    if attribute.mutability == Mutability.read_only:
                        continue
                    if attribute.type != SchemaAttribute.Type.complex:
                        if self._ask_column(model, urn, attribute):
                            clear.append(urn)
                        continue
                    if not self._ask(model, urn, column=False):
                        continue
                    for sub in attribute.sub_attributes or []:
                        sub_urn = f"{urn}.{sub.name}"
                        if (
                            not sub.multi_valued
                            and sub.mutability != Mutability.read_only
                            and self._ask_column(model, sub_urn, sub)
                        ):
                            clear.append(sub_urn)
        if clear:
            raise ValueError(
                f"{', '.join(repr(urn) for urn in clear)} are write-only: store "
                "them with a column type that hashes them, such as Hashed, or "
                "not at all"
            )

    def _ask_column(self, model: Any, urn: str, attribute: Any) -> bool:
        """Ask store how to keep an attribute held by a column, and tell whether it is a write-only string kept in clear text."""
        if attribute.mutability == Mutability.write_only and not self._hashed(
            attribute
        ):
            return False
        decision = self._ask(model, urn, column=True)
        return self._hashed(attribute) and decision is True

    def _ask(self, model: Any, urn: str, *, column: bool) -> bool | TypeEngine[Any]:
        """Ask store how to keep an attribute, and refuse a column type for an attribute without its own column."""
        decision = self.store(Path(urn).resolve(model))  # type: ignore[arg-type]
        if not column and not isinstance(decision, bool):
            raise ValueError(
                f"{urn!r} has no column of its own: store it or not, without a "
                "column type"
            )
        self.decisions[urn] = decision
        return decision

    @staticmethod
    def _hashed(attribute: Any) -> bool:
        """Tell whether an attribute is a single write-only string, whose hash can be stored."""
        return bool(
            attribute.mutability == Mutability.write_only
            and attribute.type == SchemaAttribute.Type.string
            and not attribute.multi_valued
        )

    def _linked(self, attribute: Any) -> list[str]:
        """Return the served resource types an attribute links to, through the referenceTypes of its $ref."""
        if attribute.type != SchemaAttribute.Type.complex:
            return []
        ref = _sub_attribute(attribute, "$ref")
        if ref is None:
            return []
        return [name for name in ref.reference_types or [] if name in self.tables]

    def _find_forwards(self, owner: str) -> None:
        """Find the attributes of a resource type that link to resources, and add their association tables."""
        for schema in self.schemas[owner]:
            for attribute in schema.attributes:
                targets = self._linked(attribute)
                urn = f"{schema.urn}:{attribute.name}"
                if (
                    not targets
                    or attribute.mutability == Mutability.read_only
                    or self.decisions[urn] is False
                ):
                    continue
                base = _snake(schema.column_prefix + attribute.name)
                keys = {
                    target: _fit(
                        base if len(targets) == 1 else f"{base}_{_snake(target)}"
                    )
                    for target in targets
                }
                tables = {}
                if attribute.multi_valued:
                    for target in targets:
                        tables[target] = Table(
                            _fit(
                                f"{self.tables[owner]}_{base}_"
                                f"{self.tables[target].removeprefix(self.prefix)}"
                            ),
                            self.metadata,
                            Column(
                                "resource_id",
                                Uuid,
                                ForeignKey(
                                    f"{self.tables[owner]}.id", ondelete="CASCADE"
                                ),
                                primary_key=True,
                            ),
                            Column(
                                "linked_id",
                                Uuid,
                                ForeignKey(
                                    f"{self.tables[target]}.id", ondelete="CASCADE"
                                ),
                                primary_key=True,
                            ),
                        )
                self.forwards[urn] = _Forward(
                    owner, attribute, targets, keys, tables, {}
                )

    def _find_inverses(self) -> None:
        """Pair each inverse attribute with the Link of the attribute it reverses, that links to its own resource type."""
        for reverse, forward_urn in self.inverses.items():
            forward = self.forwards.get(forward_urn)
            if forward is None or self.decisions.get(reverse) is False:
                continue
            urn, _, name = reverse.rpartition(":")
            for owner, schemas in self.schemas.items():
                for schema in schemas:
                    attribute = next(
                        (a for a in schema.attributes if a.name == name), None
                    )
                    if (
                        schema.urn != urn
                        or attribute is None
                        or not attribute.multi_valued
                        or owner not in forward.tables
                        or forward.owner not in self._linked(attribute)
                    ):
                        continue
                    forward.inverse_keys[owner] = _snake(
                        schema.column_prefix + attribute.name
                    )
                    self.reverses[reverse] = forward

    def _build(self, name: str) -> _Class:
        """Return the class of a resource type, with its columns and relationships, and the attributes of its mapping."""
        record_class = _Class(name, self.tables[name])
        version = mapped_column(Integer, nullable=False)
        for key, value, path in [
            ("id", mapped_column(Uuid, primary_key=True, default=uuid.uuid4), "id"),
            ("external_id", mapped_column(String), "externalId"),
            (
                "meta_created",
                mapped_column(DateTime(timezone=True), nullable=False),
                "meta.created",
            ),
            (
                "meta_last_modified",
                mapped_column(DateTime(timezone=True), nullable=False),
                "meta.lastModified",
            ),
            ("meta_version", version, "meta.version"),
        ]:
            record_class.add(key, value, path)
            if key != "meta_version":
                record_class.attributes.append((path, self._getter(name, key)))
        record_class.namespace["__mapper_args__"] = {"version_id_col": version}

        for schema in self.schemas[name]:
            for attribute in schema.attributes:
                path = f"{schema.path_prefix}{attribute.name}"
                key = _snake(schema.column_prefix + attribute.name)
                attribute_urn = f"{schema.urn}:{attribute.name}"
                if self.decisions.get(attribute_urn) is False:
                    continue
                if attribute_urn in self.forwards:
                    self._link(name, record_class, path, self.forwards[attribute_urn])
                elif attribute_urn in self.reverses:
                    self._reverse(
                        name,
                        record_class,
                        path,
                        attribute,
                        key,
                        self.reverses[attribute_urn],
                    )
                elif attribute.mutability == Mutability.read_only:
                    continue
                elif attribute.type == SchemaAttribute.Type.complex:
                    self._complex(
                        name, record_class, path, attribute, key, attribute_urn
                    )
                elif attribute.multi_valued:
                    self._values(
                        name, record_class, path, attribute, key, attribute_urn
                    )
                else:
                    required = schema.path_prefix == "" and (
                        attribute.required == Required.true
                    )
                    mapped = self._column(
                        name,
                        record_class,
                        path,
                        attribute,
                        key,
                        attribute_urn,
                        nullable=not required,
                        unique=attribute.uniqueness
                        in (Uniqueness.server, Uniqueness.global_),
                    )
                    if mapped is not None:
                        record_class.attributes.append((path, mapped))
        return record_class

    def _make(self, record_class: _Class) -> None:
        """Create the record class from its namespace."""
        self.classes[record_class.name] = type(
            record_class.name, (self.base,), record_class.namespace
        )

    def _getter(self, name: str, key: str) -> Callable[[], Any]:
        """Return a function reading an attribute of a record class once it is created."""
        return lambda: getattr(self.classes[name], key)

    def _class(self, name: str) -> Callable[[], Any]:
        """Return a function reading a record class once it is created."""
        return lambda: self.classes[name]

    def _join(self, name: str, column: Any) -> Callable[[], Any]:
        """Return the condition joining the records of a resource type to a column of an association table."""
        return lambda: self.classes[name].id == column

    def _column(
        self,
        name: str,
        record_class: _Class,
        path: str,
        attribute: Any,
        key: str,
        urn: str,
        *,
        nullable: bool = True,
        unique: bool = False,
    ) -> Callable[[], Any] | None:
        """Add the column of a simple attribute, and return what maps it, or None when it is not stored."""
        decision = self.decisions.get(urn, False)
        if decision is False:
            return None
        key = _fit(key)
        record_class.add(
            key,
            mapped_column(
                self._type(attribute, decision),
                nullable=nullable,
                unique=unique or None,
            ),
            path,
        )
        getter = self._getter(name, key)
        if attribute.mutability != Mutability.write_only:
            return getter
        return lambda: Attribute(getter(), readable=False)

    @staticmethod
    def _type(attribute: Any, decision: bool | TypeEngine[Any]) -> TypeEngine[Any]:
        """Return the column type store gave, or the type matching the attribute."""
        if isinstance(decision, TypeEngine):
            return decision
        return _SQL_TYPES[attribute.type]()

    def _complex(
        self,
        name: str,
        record_class: _Class,
        path: str,
        attribute: Any,
        key: str,
        urn: str,
    ) -> None:
        """Add the columns of the sub-attributes of a single-valued complex attribute, or its child table if it is multi-valued."""
        if attribute.multi_valued:
            self._entries(name, record_class, path, attribute, key, urn)
            return
        for sub in attribute.sub_attributes or []:
            if sub.multi_valued or sub.mutability == Mutability.read_only:
                continue
            sub_path = f"{path}.{sub.name}"
            mapped = self._column(
                name,
                record_class,
                sub_path,
                sub,
                f"{key}_{_snake(sub.name)}",
                f"{urn}.{sub.name}",
            )
            if mapped is not None:
                record_class.attributes.append((sub_path, mapped))

    def _child(
        self, name: str, record_class: _Class, path: str, key: str
    ) -> tuple[_Class, str]:
        """Start the class of the child table of an attribute, and add the relationship to it."""
        key = _fit(key)
        child_name = _fit(f"{self.tables[name]}_{key}")
        child = _Class(child_name, child_name)
        child.add("id", mapped_column(Integer, primary_key=True), path)
        child.add(
            "resource_id",
            mapped_column(
                Uuid,
                ForeignKey(f"{self.tables[name]}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            path,
        )
        record_class.add(
            key,
            relationship(
                self._class(child_name),
                cascade="all, delete-orphan",
                order_by=self._getter(child_name, "id"),
            ),
            path,
        )
        return child, key

    def _entries(
        self,
        name: str,
        record_class: _Class,
        path: str,
        attribute: Any,
        key: str,
        urn: str,
    ) -> None:
        """Add the child table of a multi-valued complex attribute, with a column for each stored sub-attribute."""
        child, key = self._child(name, record_class, path, key)
        columns = {}
        for sub in attribute.sub_attributes or []:
            if sub.multi_valued or sub.mutability == Mutability.read_only:
                continue
            mapped = self._column(
                child.name,
                child,
                f"{path}.{sub.name}",
                sub,
                _snake(sub.name),
                f"{urn}.{sub.name}",
            )
            if mapped is not None:
                columns[sub.name] = mapped
        if not columns:
            del record_class.namespace[key]
            return
        self._make(child)
        getter = self._getter(name, key)
        record_class.attributes.append(
            (
                path,
                lambda: Many(
                    getter(), {sub: mapped() for sub, mapped in columns.items()}
                ),
            )
        )

    def _values(
        self,
        name: str,
        record_class: _Class,
        path: str,
        attribute: Any,
        key: str,
        urn: str,
    ) -> None:
        """Add the child table of a multi-valued simple attribute, with a value column."""
        if attribute.mutability == Mutability.write_only:
            return
        child, key = self._child(name, record_class, path, key)
        child.add(
            "value",
            mapped_column(self._type(attribute, self.decisions[urn]), nullable=False),
            path,
        )
        self._make(child)
        getter = self._getter(name, key)
        value = self._getter(child.name, "value")
        record_class.attributes.append((path, lambda: Many(getter(), value())))

    def _link(
        self, name: str, record_class: _Class, path: str, forward: _Forward
    ) -> None:
        """Add a relationship for each resource type an attribute links to: an association table for a multi-valued attribute, a foreign key otherwise."""
        attribute = forward.attribute
        type_values = (_sub_attribute(attribute, "type") or attribute).canonical_values
        typed = bool(type_values) and all(
            target in type_values for target in forward.targets
        )
        links = []
        for target in forward.targets:
            key = forward.keys[target]
            if attribute.multi_valued:
                table = forward.tables[target]
                value = relationship(
                    self._class(target),
                    secondary=table,
                    primaryjoin=self._join(name, table.c.resource_id),
                    secondaryjoin=self._join(target, table.c.linked_id),
                    back_populates=forward.inverse_keys.get(target),
                )
            else:
                column_key = _fit(f"{key}_id")
                record_class.add(
                    column_key,
                    mapped_column(
                        Uuid,
                        ForeignKey(f"{self.tables[target]}.id", ondelete="SET NULL"),
                    ),
                    path,
                )
                # A resource linking to a resource of its own type, such as a
                # user managed by another user, may form a cycle, which the
                # unit of work only stores with a second UPDATE.
                value = relationship(
                    self._class(target),
                    foreign_keys=self._getter(name, column_key),
                    remote_side=self._getter(target, "id") if target == name else (),
                    post_update=target == name,
                )
            record_class.add(key, value, path)
            links.append((self._getter(name, key), target))
        record_class.attributes.append(
            (
                path,
                lambda: [
                    Link(getter(), target, type=target if typed else None)
                    for getter, target in links
                ],
            )
        )

    def _reverse(
        self,
        name: str,
        record_class: _Class,
        path: str,
        attribute: Any,
        key: str,
        forward: _Forward,
    ) -> None:
        """Add the relationship of an inverse attribute, which reads the association table of the attribute it reverses."""
        table = forward.tables[name]
        record_class.add(
            key,
            relationship(
                self._class(forward.owner),
                secondary=table,
                primaryjoin=self._join(name, table.c.linked_id),
                secondaryjoin=self._join(forward.owner, table.c.resource_id),
                back_populates=forward.keys[name],
            ),
            path,
        )
        type_values = (_sub_attribute(attribute, "type") or attribute).canonical_values
        link_type = "direct" if "direct" in (type_values or []) else None
        getter = self._getter(name, key)
        record_class.attributes.append(
            (path, lambda: Link(getter(), forward.owner, type=link_type))
        )


def _store_all(binding: AttributeBinding) -> bool:
    """Store each attribute with the column type matching it."""
    return True


def generate_mappings(
    provider: ScimProvider,
    metadata: MetaData,
    *,
    prefix: str = "",
    store: Callable[[AttributeBinding], bool | TypeEngine[Any]] = _store_all,
    inverses: Mapping[str, str] | None = None,
) -> dict[str, ResourceMapping]:
    """Generate the tables and the mappings that store the resources of a provider.

    Each resource type gets a table named after its endpoint, such as
    ``users``. A simple attribute gets a column, a complex attribute holding a
    single value gets a column for each sub-attribute, and an extension gets
    columns prefixed by the name of its schema. A multi-valued attribute gets a
    child table, mapped with :class:`~scim2_sqlalchemy.Many`. A complex
    attribute whose ``$ref`` links to served resource types gets a
    :class:`~scim2_sqlalchemy.Link` for each of them.

    A read-only attribute is not stored, except an inverse: it reads the links
    of the attribute it reverses, such as ``groups`` of a user, which reads the
    ``members`` of the groups. A multi-valued sub-attribute is not stored
    either, nor a write-only attribute other than a single string. A column
    holds ``NOT NULL`` for a required attribute of the core schema, and
    ``UNIQUE`` for an attribute unique on the server. The table and column
    names longer than 63 characters are shortened.

    ``store`` decides how each attribute is kept. It receives the
    :class:`~scim2_models.AttributeBinding` of the attribute, and returns:

    - :data:`True`, to store the attribute with the column type matching it;
    - :data:`False`, not to store it: the published schemas leave it out;
    - a column type, such as :class:`~scim2_sqlalchemy.Hashed`, to store it
      in a column of this type. Only an attribute with a column of its own
      takes one: a simple attribute, a sub-attribute, or the values of a
      multi-valued simple attribute.

    A write-only string, such as a password, is never stored in clear text:
    ``store`` gives it a column type that hashes it, or does not store it.

    Pass the mappings to the storage, and create the tables with
    :meth:`MetaData.create_all <sqlalchemy.schema.MetaData.create_all>` or a
    migration tool.

    :param provider: The provider of the server, which gives the resource types
        and their models.
    :param metadata: The :class:`~sqlalchemy.schema.MetaData` the tables are
        added to.
    :param prefix: A prefix of the names of the tables.
    :param store: The function deciding how each attribute is kept. By
        default, every attribute is stored with the column type matching it.
    :param inverses: The read-only attributes reading the links of another
        attribute, by URN, in addition to ``groups`` of a user reversing
        ``members`` of a group.
    :returns: The mappings, by resource type name.
    :raises ValueError: When ``store`` keeps a write-only string in clear
        text, when it gives a column type to an attribute without a column of
        its own, or when two attributes need the same column name.
    """
    return _Generator(
        provider,
        metadata,
        prefix,
        store,
        {_GROUPS: _MEMBERS, **(inverses or {})},
    ).generate()
