import datetime
from collections.abc import Mapping
from enum import Enum
from inspect import isclass
from typing import Any

from pydantic import BaseModel
from scim2_models import InvalidValueException
from scim2_models import Meta
from scim2_models import Mutability
from scim2_models import MutabilityException
from scim2_models import Path
from scim2_models import Resource

from .mapping import ResourceMapping
from .mapping import _Collection
from .mapping import _Column
from .mapping import _Storage


def _as_scim(value: Any) -> Any:
    """Return a value read from the database in the form SCIM holds it.

    A date without a time zone is taken as UTC, since SQLite drops the zone.
    """
    if isinstance(value, datetime.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value


def _as_stored(value: Any) -> Any:
    """Return a SCIM value in the form a column holds it."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        return value.astimezone(datetime.UTC)
    return value


def _read(column: _Column, record: Any) -> Any:
    value = getattr(record, column.key)
    if value is None or not column.readable:
        return None
    return _as_scim(value)


def _version(mapping: ResourceMapping, record: Any) -> str:
    return f'W/"{getattr(record, mapping._version.key)}"'


def _to_scim(
    mapping: ResourceMapping,
    record: Any,
    resource_type: str,
    model: type[Resource[Any]],
    endpoints: Mapping[str, str],
) -> Resource[Any]:
    """Build the resource a record stores, as an instance of model.

    The attributes are found by their URN, so model may be another model of
    the same schemas than the model of the mapping, such as a model a provider
    built from the schemas. endpoints gives the endpoint of each resource type
    by name: a link to one of them gets a $ref relative to the SCIM root, when
    model declares it, which the server makes absolute (RFC 7643 §2.3.7).
    """
    resource = model()
    special = (mapping._id, mapping._created, mapping._last_modified)
    for column in mapping._columns.values():
        if column in special:
            continue
        value = _read(column, record)
        if value is not None:
            Path[model](column.binding.urn).set(resource, value)  # type: ignore[valid-type]

    for collection in mapping._collections.values():
        endpoint = None
        if Path[model](f"{collection.binding.urn}.$ref").resolve() is not None:  # type: ignore[valid-type]
            endpoint = endpoints.get(collection.resource_type)  # type: ignore[arg-type]
        entries = [
            _read_entry(collection, entry, endpoint)
            for entry in _related(collection, record)
        ]
        if entries:
            value = entries[0] if collection.single else entries
            Path[model](collection.binding.urn).set(resource, value)  # type: ignore[valid-type]

    resource.id = str(_read(mapping._id, record))
    resource.meta = Meta(
        resource_type=resource_type,
        created=_read(mapping._created, record),
        last_modified=_read(mapping._last_modified, record),
        version=_version(mapping, record),
    )
    return model.model_validate(resource.model_dump())


def _related(collection: _Collection, record: Any) -> list[Any]:
    """Return the records a collection of a record holds, as a list even for a single one."""
    related = getattr(record, collection.relationship.key)
    if collection.single:
        return [] if related is None else [related]
    return list(related)


def _entries(collection: _Collection, resource: Resource[Any]) -> list[Any]:
    """Return the entries a resource holds for a collection, as a list even for a single one."""
    value = Path(collection.binding.urn).get(resource, strict=False)
    if collection.single:
        return [] if value is None else [value]
    return list(value or [])


def _read_entry(
    collection: _Collection, entry: Any, endpoint: str | None
) -> dict[str, Any]:
    """Return the values of an entry, by sub-attribute name.

    A link gets a $ref when the endpoint of the resources it links to is known.
    """
    values = {}
    for sub_field_name, column in collection.columns.items():
        value = _read(column, entry)
        if value is not None and sub_field_name == "value" and not collection.owned:
            value = str(value)
            if endpoint is not None:
                values["$ref"] = f"{endpoint}/{value}"
        values[column.binding.urn.rsplit(".", 1)[-1]] = value
    return values


def _from_scim(
    mapping: ResourceMapping,
    resource: Resource[Any],
    record: Any,
    links: dict[_Collection, list[Any]],
    creating: bool,
) -> None:
    """Write a resource into a record.

    Read-only attributes are left as they are, and so is a write-only attribute
    the resource has no value for, since a resource never carries it back.
    When creating the record, a missing value is not written, so that the
    column keeps its default. links holds the records each Link of the
    resource links to.
    """
    for column in mapping._columns.values():
        if not column.writable:
            continue
        value = Path(column.binding.urn).get(resource, strict=False)
        if value is None and (creating or not column.readable):
            continue
        setattr(record, column.key, None if value is None else _as_stored(value))

    for collection in mapping._collections.values():
        if not collection.writable:
            continue
        if collection.owned:
            entries = Path(collection.binding.urn).get(resource, strict=False) or []
            value = [_write_entry(collection, entry) for entry in entries]
        elif collection.single:
            value = next(iter(links[collection]), None)
        else:
            value = links[collection]
        setattr(record, collection.relationship.key, value)


def _write_entry(collection: _Collection, entry: Any) -> Any:
    record = collection.entry_class()
    for sub_field_name, column in collection.columns.items():
        if not column.writable:
            continue
        value = getattr(entry, sub_field_name, None)
        if value is not None:
            setattr(record, column.key, _as_stored(value))
    return record


def _link_ids(
    mapping: ResourceMapping, resource: Resource[Any]
) -> dict[_Collection, list[str]]:
    """Return the identifiers each writable Link of a resource links to.

    A link is stored by its value, so an entry without one, such as an entry
    with a $ref only, raises InvalidValueException rather than being lost.
    """
    links = {}
    for collection in mapping._collections.values():
        if collection.owned or not collection.writable:
            continue
        entries = _entries(collection, resource)
        if any(entry.value is None for entry in entries):
            raise InvalidValueException(
                detail=f"'{collection.binding.urn}' holds an entry without value"
            )
        links[collection] = [entry.value for entry in entries]
    return links


def _check_storable(
    mapping: ResourceMapping,
    resource: Resource[Any],
    stored: Resource[Any] | None,
) -> None:
    """Refuse a change of a resource that the mapping cannot store.

    Each attribute of the resource is compared with the stored resource, None
    for a new one. A value equal to the stored one changes nothing, so a
    resource read and sent back is accepted. A missing value changes nothing
    either, except for a stored value whose column cannot hold none: removing it
    raises MutabilityException. The read-only attributes of the model of the resource are skipped,
    as RFC 7644 §3.5.1 has them ignored, and so are the derived ones.
    """
    model = type(resource)
    for path in Path[model].iter_paths():  # type: ignore[valid-type]
        binding = path.resolve()
        if binding is None:
            continue
        target = binding.target_type
        if isclass(target) and issubclass(target, BaseModel):
            continue
        if Mutability.read_only in (
            binding.model.get_field_annotation(binding.field_name, Mutability),
            binding.get_annotation(Mutability),
        ):
            continue
        storage = mapping._storage(binding.urn)
        value = _comparable(path.get(resource, strict=False))
        current = None
        if stored is not None:
            current = _comparable(Path(binding.urn).get(stored, strict=False))
        if storage == _Storage.written:
            if (
                value is None
                and current is not None
                and not mapping._nullable(binding.urn)
            ):
                raise MutabilityException(
                    attribute=binding.urn,
                    detail=f"'{binding.urn}' cannot be removed on this server",
                )
            continue
        if storage in (_Storage.derived, _Storage.ref) or value in (None, current):
            continue
        if storage == _Storage.read_only:
            raise MutabilityException(
                attribute=binding.urn,
                mutability="readOnly",
                detail=f"'{binding.urn}' is read-only on this server",
            )
        raise InvalidValueException(
            detail=f"'{binding.urn}' is not stored by this server"
        )


def _comparable(value: Any) -> Any:
    """Return a value in a form two resources compare in.

    A list of missing values, as a path through the entries of a multi-valued
    attribute gives, is no value.
    """
    if isinstance(value, list):
        values = [_as_stored(item) for item in value]
        return None if all(item is None for item in values) else values
    return _as_stored(value)
