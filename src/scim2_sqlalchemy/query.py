import decimal
import operator
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any
from typing import TypeVarTuple
from typing import cast as cast_type

from scim2_models import AttributeBinding
from scim2_models import InvalidCursorException
from scim2_models import InvalidPathException
from scim2_models import ResponseParameters
from scim2_models import SearchRequest
from scim2_models.path import CompareOperator
from scim2_server.keyset import Position
from scim2_server.mapping import Collection
from scim2_server.mapping import Column
from scim2_server.mapping.filters import entry_present
from scim2_server.mapping.filters import holds_any
from scim2_server.mapping.filters import sort_target
from scim2_server.mapping.filters import where
from sqlalchemy import BigInteger
from sqlalchemy import ColumnElement
from sqlalchemy import Double
from sqlalchemy import Integer
from sqlalchemy import Numeric
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import bindparam
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import false
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import literal
from sqlalchemy import not_
from sqlalchemy import null
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import true
from sqlalchemy import union_all
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapper
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import TypeEngine

from .comparison import _SUBSTRINGS
from .comparison import _Comparator
from .conversion import _as_stored
from .keyset import _beyond
from .keyset import _keyset_order
from .keyset import _Sorted
from .mapping import _INTEGERS
from .mapping import _MARIADB
from .mapping import ResourceMapping
from .mapping import _as_key
from .mapping import _Collection
from .mapping import _Column
from .mapping import _python_type

_ORDERINGS: dict[CompareOperator, Callable[[Any, Any], Any]] = {
    CompareOperator.eq: operator.eq,
    CompareOperator.gt: operator.gt,
    CompareOperator.ge: operator.ge,
    CompareOperator.lt: operator.lt,
    CompareOperator.le: operator.le,
}


_Row = TypeVarTuple("_Row")

_FAMILIES: dict[type | None, type] = {float: int, decimal.Decimal: int}
"""The Python types of sort values that a UNION puts in one column, by the type standing for them."""

_DECIMAL = Numeric().with_variant(Numeric(65, 30), *_MARIADB)
"""The type of decimal sort values in a UNION. MariaDB would otherwise keep no digit after the point."""


class _TypedNull(ColumnElement[Any]):
    """A NULL of a type, which PostgreSQL needs to unite it with the values of other tables."""

    inherit_cache = True
    _traverse_internals = [("type", InternalTraversal.dp_type)]

    def __init__(self, type_: Any) -> None:
        self.type = type_


@compiles(_TypedNull)
def _compile_typed_null(element: _TypedNull, compiler: SQLCompiler, **kw: Any) -> str:
    """Write the NULL cast to its type."""
    return compiler.process(cast(null(), element.type), **kw)


@compiles(_TypedNull, "mariadb")
@compiles(_TypedNull, "mysql")
def _compile_typed_null_mariadb(
    element: _TypedNull, compiler: SQLCompiler, **kw: Any
) -> str:
    """Write a bare NULL, since MariaDB cannot cast to every type, such as a boolean."""
    return "NULL"


def _bound(column: _Column, value: Any) -> Any:
    """Return a value ready to compare with a column.

    An integer is sent to a column of integers as a BIGINT. PostgreSQL would
    otherwise cast it to the type of the column, and fail when it does not
    fit. A column of another type, such as a TypeDecorator, gets the value as
    it is, to convert it itself.
    """
    if isinstance(column.expression.type, Integer) and isinstance(value, int):
        return literal(value, BigInteger)
    return value


def _among(column: _Column, keys: list[Any]) -> ColumnElement[bool]:
    """Return the condition that a column holds one of the keys, sent as _bound sends them."""
    if not isinstance(column.expression.type, Integer):
        return column.expression.in_(keys)
    return column.expression.in_(
        bindparam("keys", keys, expanding=True, type_=BigInteger, unique=True)
    )


def _out_of_range(
    expression: ColumnElement[Any], op: CompareOperator, value: int
) -> ColumnElement[bool]:
    """Compare an integer column with an integer no database can store."""
    above = value > 0
    if (op in (CompareOperator.gt, CompareOperator.ge) and not above) or (
        op in (CompareOperator.lt, CompareOperator.le) and above
    ):
        return expression.is_not(None)
    return false()


def _exact_key(column: _Column, value: str) -> Any:
    """Return an identifier in the type of its column, or None when the column holds no such identifier.

    The column holds it only when it would return the same text, so "01"
    does not find the identifier 1.
    """
    key = _as_key(column, value)
    if key is None or str(key) != value:
        return None
    return key


class _Predicates:
    """The conditions of the mapping core, as SQLAlchemy expressions.

    Every condition is true or false, never NULL, so that the negation of a
    comparison with a missing value holds. The conditions on the entries of a
    collection compare the columns of the entry class, which the relationship
    correlates with the record.
    """

    def __init__(self, comparator: _Comparator) -> None:
        self.comparator = comparator

    def true(self) -> ColumnElement[bool]:
        return true()

    def false(self) -> ColumnElement[bool]:
        return false()

    def any(self, conditions: Iterable[ColumnElement[bool]]) -> ColumnElement[bool]:
        return or_(false(), *conditions)

    def all(self, conditions: Iterable[ColumnElement[bool]]) -> ColumnElement[bool]:
        return and_(true(), *conditions)

    def negate(self, condition: ColumnElement[bool]) -> ColumnElement[bool]:
        return not_(condition)

    def is_null(self, column: Column) -> ColumnElement[bool]:
        return _sa(column).compared.is_(None)

    def is_empty(self, column: Column) -> ColumnElement[bool]:
        return _sa(column).compared == ""

    def compare(
        self, column: Column, op: CompareOperator, value: Any
    ) -> ColumnElement[bool]:
        """Compare a column that does not hold strings.

        An integer no database stores is above or below every value. A
        boolean is compared with a column of integers as 0 or 1, since
        PostgreSQL has no operator between an integer and a boolean.
        """
        column = _sa(column)
        expression = column.compared
        value = _as_stored(value)
        if isinstance(value, int) and _python_type(column.expression) is int:
            if value not in _INTEGERS:
                return _out_of_range(expression, op, value)
            value = _bound(column, int(value))
        return _ORDERINGS[op](expression, value)  # type: ignore[no-any-return]

    def compare_text(
        self, column: Column, op: CompareOperator, value: str
    ) -> ColumnElement[bool]:
        """Compare a string column in the form the policy compares it under.

        A stored value the comparison key refuses is compared as no value, so
        that the comparison is false rather than NULL.
        """
        column = _sa(column)
        left = self.comparator.column(column)
        if op in _SUBSTRINGS:
            condition = self.comparator.substring(op, column, value)
        else:
            condition = _ORDERINGS[op](left, self.comparator.operand(column, value))
        return and_(left.is_not(None), condition)

    def compare_key(self, column: Column, value: Any) -> ColumnElement[bool] | None:
        """Compare an identifier in its own type.

        The database then uses its index, and the result does not depend on
        how the database writes a UUID as text.
        """
        column = _sa(column)
        key = _exact_key(column, value)
        if key is None:
            return None
        return column.expression == _bound(column, key)  # type: ignore[no-any-return]

    def exists(
        self,
        collection: Collection,
        build: Callable[[Any], ColumnElement[bool]] | None = None,
    ) -> ColumnElement[bool]:
        condition = None if build is None else build(self)
        return collection.exists(condition)  # type: ignore[attr-defined,no-any-return]


def _sa(column: Column) -> _Column:
    """Return a column built by the SQLAlchemy mapping."""
    return column  # type: ignore[return-value]


def _sort_key(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
) -> Any:
    """Return the expression the records of a mapping sort on.

    Return None when the search has no sortBy, or when the model of the
    mapping does not declare its attribute. A constant of an Entry sorts as
    its value when the entry exists, and as no value otherwise.
    """
    target = sort_target(mapping, search_request)
    if target is None:
        return None
    collection, column = target
    if column.is_constant:
        return _constant_key(mapping, column, comparator)
    expression: Any = _sa(column).expression
    if column.is_string:
        expression = comparator.column(_sa(column))
    if collection is not None:
        expression = _first_entry(collection, expression)  # type: ignore[arg-type]
    return expression


def _constant_key(
    mapping: ResourceMapping, column: Column, comparator: _Comparator
) -> Any:
    """Return the sort key of a constant of an Entry: its comparison key when the entry exists, and no value otherwise."""
    entry = mapping._entry_of(column)
    present = entry_present(_Predicates(comparator), entry)  # type: ignore[arg-type]
    constant = column.constant
    if column.is_string:
        constant = column.binding.comparable(constant, comparator.policy)
    return case((present, literal(constant)), else_=null())


def _first_entry(collection: _Collection, expression: Any) -> Any:
    """Return the value of the primary entry of a collection, or else of its first one."""
    relationship = collection.relationship_property
    entry_mapper: Mapper[Any] = inspect(collection.entry_class)
    order: list[Any] = []
    primary = collection.columns.get("primary")
    if primary is not None:
        order.append(_Sorted(_sa(primary).expression, ascending=False, last=True))
    order.extend(relationship.order_by or entry_mapper.primary_key)
    return (
        select(expression)
        .where(relationship.primaryjoin)
        .order_by(*order)
        .limit(1)
        .scalar_subquery()
    )


def _where(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
) -> ColumnElement[bool]:
    """Return the condition keeping the records matching the filter of a search, or every record without filter."""
    condition: ColumnElement[bool] = where(
        mapping, search_request, _Predicates(comparator), comparator.policy
    )
    return condition


def _cursor_page(
    page: Select[*_Row], search_request: SearchRequest[Any]
) -> Select[*_Row]:
    """Limit a cursor page to count + 1 rows, the last one telling whether a page follows."""
    if search_request.count is None:
        return page
    return page.limit(search_request.count + 1)


def _index_page(
    page: Select[*_Row], search_request: SearchRequest[Any]
) -> Select[*_Row]:
    """Limit a page to the records from startIndex, at most count of them."""
    page = page.offset(search_request.start_index_0 or 0)
    if search_request.count is None:
        return page
    return page.limit(search_request.count)


def _search_statements(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
    anchor: Position | None = None,
) -> tuple[Select[int], Select[Any, Any, str]]:
    """Return the statement counting the matching records, and the one selecting a page of them.

    The page lists each record with its sort value and its identifier as
    text. The records are ordered by sort value, then by identifier: without
    the identifier, records sharing a sort value could change pages from a
    request to the next. A cursor page is read from the anchor, in the reverse
    order for a previous page.
    """
    condition = _where(mapping, search_request, comparator)
    count = select(func.count()).select_from(mapping.record).where(condition)
    key = _sort_key(mapping, search_request, comparator)
    record_id = mapping._id.expression
    descending = search_request.sort_order == SearchRequest.SortOrder.descending
    forward = anchor is None or anchor.forward
    page = (
        select(
            mapping.record,
            (null() if key is None else key).label("key"),
            mapping._id.text.label("id"),
        )
        .where(condition)
        .order_by(*_keyset_order(key, [record_id], descending, forward))
        .options(
            *mapping._loader_options(mapping._returned_collections(search_request))
        )
    )
    if search_request.cursor is None:
        return count, _index_page(page, search_request)
    if anchor is not None:
        value = _as_key(mapping._id, anchor.id)
        if value is None:
            raise InvalidCursorException
        page = page.where(
            _beyond(key, [record_id], anchor, [_bound(mapping._id, value)], descending)
        )
    return count, _cursor_page(page, search_request)


def _root_statements(
    mappings: list[ResourceMapping],
    search_request: SearchRequest[Any],
    comparator: _Comparator,
    anchor: Position | None = None,
) -> tuple[Select[int], Select[int, str, Any]]:
    """Return the statement counting the matching records of several mappings, and the one selecting a page of them.

    The page lists the position of the mapping, the identifier of each
    record as text, so that the identifiers of every table fit in one column,
    and its sort value. A mapping whose model does not declare the sort
    attribute sorts its records as having no value.
    """
    conditions = [_where(mapping, search_request, comparator) for mapping in mappings]
    keys = [_sort_key(mapping, search_request, comparator) for mapping in mappings]
    types = {_python_type(key) for key in keys if key is not None} - {None}
    if len({_FAMILIES.get(python_type, python_type) for python_type in types}) > 1:
        raise InvalidPathException(
            path=str(search_request.sort_by),
            detail=f"Cannot sort on '{search_request.sort_by}': "
            "the resource types store it in different types",
        )
    if len(types) > 1:
        # The column of the union takes the type of the first key, which
        # would read and bind the numbers of the other keys in that type.
        common: TypeEngine[Any] = _DECIMAL if decimal.Decimal in types else Double()
        keys = [None if key is None else cast(key, common) for key in keys]
    key_type = next((key.type for key in keys if key is not None), None)

    matching = union_all(
        *(
            select(literal(1)).select_from(mapping.record).where(condition)
            for mapping, condition in zip(mappings, conditions, strict=True)
        )
    ).subquery()
    count = select(func.count()).select_from(matching)

    branches = []
    for position, (mapping, condition, key) in enumerate(
        zip(mappings, conditions, keys, strict=True)
    ):
        columns: list[Any] = [
            literal(position).label("position"),
            mapping._id.text.label("id"),
        ]
        if key_type is not None:
            key = _TypedNull(key_type) if key is None else key
            columns.append(key.label("key"))
        branches.append(select(*columns).where(condition))
    rows = union_all(*branches).subquery()
    row_key = None if key_type is None else rows.c.key
    ties = [rows.c.position, rows.c.id]
    descending = search_request.sort_order == SearchRequest.SortOrder.descending
    forward = anchor is None or anchor.forward
    page = select(
        rows.c.position, rows.c.id, (null() if row_key is None else row_key)
    ).order_by(*_keyset_order(row_key, ties, descending, forward))
    if search_request.cursor is None:
        return count, _index_page(page, search_request)
    if anchor is not None:
        values = [anchor.type_index, anchor.id]
        page = page.where(_beyond(row_key, ties, anchor, values, descending))
    return count, _cursor_page(page, search_request)


def _records_statement(
    mapping: ResourceMapping, ids: list[str], parameters: ResponseParameters[Any]
) -> Select[str, Any]:
    """Return the statement loading the records of a page, each with its identifier as text."""
    keys = [_as_key(mapping._id, value) for value in ids]
    return (
        select(mapping._id.text, mapping.record)
        .where(_among(mapping._id, keys))
        .options(*mapping._loader_options(mapping._returned_collections(parameters)))
    )


def _load_statement(
    mapping: ResourceMapping,
    resource_id: str | None,
    parameters: ResponseParameters[Any] | None = None,
    locked: bool = False,
) -> Select[Any] | None:
    """Return the statement loading a record, or None when no record can have this identifier.

    Only the collections the response keeps are loaded, all of them without
    parameters. A locked record stays locked until the end of the
    transaction, on the databases that lock rows.
    """
    key = _as_key(mapping._id, resource_id)
    if key is None:
        return None
    statement = (
        select(mapping.record)
        .where(mapping._id.expression == _bound(mapping._id, key))
        .options(*mapping._loader_options(mapping._returned_collections(parameters)))
    )
    return statement.with_for_update() if locked else statement


def _id_taken_statement(
    mapping: ResourceMapping, resource_id: str
) -> Select[Any] | None:
    """Return the statement finding a record of a mapping with an identifier, or None when no record can have it."""
    key = _as_key(mapping._id, resource_id)
    if key is None:
        return None
    condition = mapping._id.expression == _bound(mapping._id, key)
    return select(mapping._id.expression).where(condition).limit(1)


def _taken_statement(
    mapping: ResourceMapping,
    binding: AttributeBinding,
    values: list[Any],
    comparator: _Comparator,
    record: Any,
) -> Select[Any]:
    """Return the statement finding a record of a mapping holding one of some unique values.

    record is the record an update writes, and None for a new resource. It is
    left out, even when it is read through another mapping of its class.
    """
    condition = holds_any(
        mapping, _Predicates(comparator), comparator.policy, binding, values
    )
    if record is not None:
        condition = and_(condition, _other_than(mapping, record))
    return select(mapping._id.expression).where(condition).limit(1)


def _other_than(mapping: ResourceMapping, record: Any) -> ColumnElement[bool]:
    """Return the condition that a record of a mapping is not the given one.

    Two classes share their records when their base classes map the same
    table, such as two classes of a table, or of an inheritance.
    """
    mapper: Mapper[Any] = inspect(mapping.record)
    state = inspect(record)
    if mapper.base_mapper.local_table is not state.mapper.base_mapper.local_table:
        return true()
    keys = zip(mapper.primary_key, state.identity, strict=True)
    return not_(and_(*(column == key for column, key in keys)))


def _links_statement(collection: Collection, ids: list[str]) -> Select[Any]:
    """Return the statement loading the records a Link links to."""
    target = cast_type(ResourceMapping, collection.target)
    keys = [
        key for key in (_as_key(target._id, value) for value in ids) if key is not None
    ]
    return select(target.record).where(_among(target._id, keys))
