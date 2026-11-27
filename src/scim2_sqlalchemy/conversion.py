import datetime
from collections.abc import Iterable
from collections.abc import Mapping
from enum import Enum
from typing import Any

from scim2_models import Resource
from scim2_server.mapping import conversion as core
from scim2_server.mapping.descriptors import Collection

from .mapping import ResourceMapping
from .mapping import _Collection


def _as_stored(value: Any) -> Any:
    """Return a SCIM value in the form a column holds it."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        return value.astimezone(datetime.UTC)
    return value


def _to_scim(
    mapping: ResourceMapping,
    record: Any,
    resource_type: str,
    model: type[Resource[Any]],
    endpoints: Mapping[str, str],
    collections: Iterable[Collection],
    version_key: bytes | None,
) -> Resource[Any]:
    """Build the resource a record stores, as an instance of model.

    Only the given collections are read: the others may not be loaded.

    The attributes are found by their URN, so model may be another model of
    the same schemas than the model of the mapping, such as a model a provider
    built from the schemas. endpoints gives the endpoint of each resource type
    by name: a link to one of them gets a $ref relative to the SCIM root, when
    model declares it, which the server makes absolute (RFC 7643 §2.3.7).
    """
    return core.to_scim(
        mapping, record, resource_type, model, endpoints, collections, version_key
    )


def _from_scim(
    mapping: ResourceMapping,
    resource: Resource[Any],
    record: Any,
    links: dict[Collection, list[Any]],
    creating: bool,
) -> None:
    """Write a resource into a record.

    Read-only attributes are left as they are. A value equal to the stored one
    is not written again, so that a hash given back as it was read is not
    hashed again. When creating the record, a missing value is not written, so
    that the column keeps its default. links holds the records each Link of
    the resource links to.
    """
    for column, value in core.write_plan(mapping, resource, record, creating):
        setattr(record, column.key, None if value is None else _as_stored(value))

    for collection in mapping._all_collections():
        if not collection.writable:
            continue
        if collection.owned:
            value = [
                _write_entry(collection, entry)  # type: ignore[arg-type]
                for entry in core.entries(collection, resource)
            ]
        elif collection.single:
            value = next(iter(links[collection]), None)
        else:
            value = links[collection]
        setattr(record, collection.relation.key, value)


def _write_entry(collection: _Collection, entry: Any) -> Any:
    """Build the record storing an entry of a multi-valued attribute."""
    record = collection.entry_class()
    if collection.simple:
        setattr(record, collection.columns[None].key, _as_stored(entry))
        return record
    for column in collection.columns.values():
        value = getattr(entry, column.binding.target_field_name, None)
        if value is not None:
            setattr(record, column.key, _as_stored(value))
    return record
