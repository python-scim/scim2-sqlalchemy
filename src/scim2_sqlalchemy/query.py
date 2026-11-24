import decimal
import operator
from collections.abc import Callable
from typing import Any
from typing import TypeVarTuple

from scim2_models import AttributeBinding
from scim2_models import InvalidCursorException
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import ResponseParameters
from scim2_models import ScimFilter
from scim2_models import SearchRequest
from scim2_models.path import AttrPath
from scim2_models.path import CompareOperator
from scim2_models.path import Comparison
from scim2_models.path import FilterVisitor
from scim2_models.path import LogicalExpr
from scim2_models.path import LogicalOperator
from scim2_models.path import Not
from scim2_models.path import Present
from scim2_models.path import ValuePath
from scim2_models.path import coerce_value
from scim2_server.keyset import Position
from sqlalchemy import BigInteger
from sqlalchemy import ColumnElement
from sqlalchemy import Double
from sqlalchemy import Integer
from sqlalchemy import Numeric
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import bindparam
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


def _as_key(column: _Column, value: Any) -> Any:
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
    if op == CompareOperator.ne:
        return true()
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


def _unmapped(binding: AttributeBinding) -> InvalidFilterException:
    """Return the error of a filter on an attribute the mapping does not let filters read."""
    return InvalidFilterException(detail=f"Cannot filter on '{binding.urn}'")


class _FilterTranslator(FilterVisitor[ColumnElement[bool]]):
    """Turn a SCIM filter into a condition on the records of a mapping.

    The condition keeps the records whose resources ScimFilter.match keeps.
    Every condition is true or false, never NULL, so that the negation of a
    comparison with a missing value holds.

    scope is the collection that a value selection, such as
    emails[type eq "work"], applies its filter to.
    """

    def __init__(
        self,
        mapping: ResourceMapping,
        scim_filter: ScimFilter[Any],
        comparator: _Comparator,
        scope: tuple[AttrPath, _Collection] | None = None,
    ) -> None:
        self.mapping = mapping
        self.filter = scim_filter
        self.comparator = comparator
        self.scope = scope

    def _resolve(
        self,
        attr_path: AttrPath,
        resolve: Callable[..., AttributeBinding | None],
    ) -> AttributeBinding | None:
        """Bind the attribute path of a comparison or of a presence test.

        Within a value selection of a simple attribute, such as
        tags[value eq "x"], value is the entry itself, and any other attribute
        matches nothing, as ScimFilter.match has it.
        """
        if self.scope is None:
            return resolve(attr_path, strict=False)
        scope_path, scope = self.scope
        if scope.simple:
            own = attr_path.attr.lower() == "value" and not attr_path.sub_attr
            return scope.binding if own else None
        return resolve(
            AttrPath(scope_path.attr, attr_path.attr, scope_path.uri), strict=False
        )

    def _lookup(
        self, binding: AttributeBinding
    ) -> list[tuple[_Collection | None, _Column | None]]:
        """Return the collections holding an attribute, with its column in each, as the mapping does.

        Within a value selection, only the collection of the selection holds
        the attribute.
        """
        pairs = self.mapping._lookup(binding)
        if self.scope is None:
            return pairs
        _, scope = self.scope
        return [
            (collection, column) for collection, column in pairs if collection is scope
        ]

    def _targets(
        self, binding: AttributeBinding
    ) -> list[tuple[_Collection | None, _Column]]:
        """Return the collections holding an attribute with its column in each, refusing an attribute that one of them does not let filters read."""
        pairs = self._lookup(binding)
        if any(column is None or not column.readable for _, column in pairs):
            raise _unmapped(binding)
        return pairs  # type: ignore[return-value]

    def _in_collection(
        self, collection: _Collection | None, condition: ColumnElement[bool]
    ) -> ColumnElement[bool]:
        """Apply a condition to the entries of a collection, or to the record itself outside of any collection.

        Within a value selection, the condition already applies to the entries.
        """
        if collection is None or self.scope is not None:
            return condition
        return collection.exists(condition)

    def visit_comparison(self, node: Comparison) -> ColumnElement[bool]:
        """Keep the records with a value matching the comparison, in any collection holding the attribute."""
        binding = self._resolve(node.attr_path, self.filter.resolve_comparison)
        if binding is None:
            return false()
        targets = self._targets(binding)
        value = coerce_value(binding, node.value, node.op)

        # 'emails ne "x"' holds when no email is "x", while
        # 'emails[value ne "x"]' holds when one email is not "x".
        if (
            node.op == CompareOperator.ne
            and targets[0][0] is not None
            and self.scope is None
        ):
            return not_(
                or_(
                    *(
                        collection.exists(  # type: ignore[union-attr]
                            _compare(self.comparator, column, CompareOperator.eq, value)
                        )
                        for collection, column in targets
                    )
                )
            )
        return or_(
            *(
                self._in_collection(
                    collection, _compare(self.comparator, column, node.op, value)
                )
                for collection, column in targets
            )
        )

    def visit_present(self, node: Present) -> ColumnElement[bool]:
        """Keep the records with a value for the attribute.

        A complex attribute is present when one of its sub-attributes is.
        """
        binding = self._resolve(node.attr_path, self.filter.resolve)
        if binding is None:
            return false()
        pairs = self._lookup(binding)
        in_collection = pairs[0][0] is not None
        if in_collection and binding.sub_field_name is None and pairs[0][1] is None:
            return or_(*(collection.exists() for collection, _ in pairs))  # type: ignore[union-attr]
        if all(column is not None and column.readable for _, column in pairs):
            return or_(
                *(
                    self._in_collection(collection, _present(column))  # type: ignore[arg-type]
                    for collection, column in pairs
                )
            )

        sub_columns = [
            column for column in self.mapping._sub_columns(binding) if column.readable
        ]
        if in_collection or not sub_columns:
            raise _unmapped(binding)
        return or_(*(_present(column) for column in sub_columns))

    def visit_not(self, node: Not) -> ColumnElement[bool]:
        """Keep the records the inner filter does not keep."""
        return not_(self.visit(node.expr))

    def visit_logical_expr(self, node: LogicalExpr) -> ColumnElement[bool]:
        """Combine the conditions of the terms with AND or OR."""
        combine = and_ if node.op == LogicalOperator.and_ else or_
        return combine(*(self.visit(term) for term in node.terms))

    def visit_value_path(self, node: ValuePath) -> ColumnElement[bool]:
        """Keep the records with an entry of the attribute matching the inner filter."""
        binding = self.filter.resolve(node.attr_path, strict=False)
        if binding is None:
            return false()
        collections = [
            collection
            for collection, _ in self.mapping._lookup(binding)
            if collection is not None
        ]
        if not collections:
            raise _unmapped(binding)
        return or_(
            *(
                collection.exists(
                    _FilterTranslator(
                        self.mapping,
                        self.filter,
                        self.comparator,
                        (node.attr_path, collection),
                    ).visit(node.val_filter)
                )
                for collection in collections
            )
        )


def _present(column: _Column) -> ColumnElement[bool]:
    """Per RFC 7644 §3.4.2.2, an empty string is no value."""
    expression = column.compared
    if column.is_string:
        return and_(expression.is_not(None), expression != "")
    return expression.is_not(None)


def _compare(
    comparator: _Comparator, column: _Column, op: CompareOperator, value: Any
) -> ColumnElement[bool]:
    """Compare a column with a value, strings in the form the policy compares them under.

    A string the policy cannot prepare is equal to no other value, so only ne
    holds against it.
    """
    expression = column.compared
    value = _as_stored(value)
    if value is None:
        if op == CompareOperator.eq:
            return expression.is_(None)
        if op == CompareOperator.ne:
            return expression.is_not(None)
        return false()

    if column.textual and op in (CompareOperator.eq, CompareOperator.ne):
        # The identifier is compared in its own type, so that the database
        # uses its index, and does not depend on how it writes a UUID as text.
        key = _exact_key(column, value)
        if key is None:
            return true() if op == CompareOperator.ne else false()
        if op == CompareOperator.ne:
            return or_(
                column.expression.is_(None), column.expression != _bound(column, key)
            )
        return and_(
            column.expression.is_not(None), column.expression == _bound(column, key)
        )

    if not column.is_string:
        if isinstance(value, int) and _python_type(column.expression) is int:
            if value not in _INTEGERS:
                return _out_of_range(expression, op, value)
            # A boolean is compared with a column of integers as 0 or 1, since
            # PostgreSQL has no operator between an integer and a boolean.
            value = _bound(column, int(value))
        if op == CompareOperator.ne:
            return or_(expression.is_(None), expression != value)
        if op in _SUBSTRINGS:
            return false()
        return and_(expression.is_not(None), _ORDERINGS[op](expression, value))

    try:
        column.binding.comparable(value, comparator.policy)
    except ValueError:
        return true() if op == CompareOperator.ne else false()

    left = comparator.column(column)
    if op == CompareOperator.ne:
        return or_(left.is_(None), left != comparator.operand(column, value))
    if op in _SUBSTRINGS:
        condition = comparator.substring(op, column, value)
    else:
        condition = _ORDERINGS[op](left, comparator.operand(column, value))
    return and_(left.is_not(None), condition)


def _sort_key(
    mapping: ResourceMapping,
    search_request: SearchRequest[Any],
    comparator: _Comparator,
) -> Any:
    """Return the expression the records of a mapping sort on.

    Return None when the search has no sortBy, or when the model of the
    mapping does not declare its attribute.
    """
    binding = search_request.sort_binding(mapping.model)
    if binding is None:
        return None
    collection, column = mapping._lookup(binding)[0]
    if (
        column is None
        or not column.readable
        or (collection is not None and not collection.owned)
    ):
        raise InvalidPathException(
            path=str(search_request.sort_by),
            detail=f"Cannot sort on '{binding.urn}'",
        )
    expression: Any = column.expression
    if column.is_string:
        expression = comparator.column(column)
    if collection is not None:
        expression = _first_entry(collection, expression)
    return expression


def _first_entry(collection: _Collection, expression: Any) -> Any:
    """Return the value of the primary entry of a collection, or else of its first one."""
    relationship = collection.relationship_property
    entry_mapper: Mapper[Any] = inspect(collection.entry_class)
    order: list[Any] = []
    primary = collection.columns.get("primary")
    if primary is not None:
        order.append(_Sorted(primary.expression, ascending=False, last=True))
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
    if search_request.filter is None:
        return true()
    scim_filter = ScimFilter[mapping.model](str(search_request.filter))  # type: ignore[name-defined]
    return _FilterTranslator(mapping, scim_filter, comparator).visit(scim_filter.ast)


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
) -> Select[Any] | None:
    """Return the statement loading a record, or None when no record can have this identifier.

    Only the collections the response keeps are loaded, all of them without parameters.
    """
    key = _as_key(mapping._id, resource_id)
    if key is None:
        return None
    return (
        select(mapping.record)
        .where(mapping._id.expression == _bound(mapping._id, key))
        .options(*mapping._loader_options(mapping._returned_collections(parameters)))
    )


def _taken_statement(
    mapping: ResourceMapping,
    column: _Column,
    value: Any,
    resource_id: str | None,
    comparator: _Comparator,
) -> Select[Any]:
    """Return the statement finding another record holding a unique value."""
    condition = _compare(comparator, column, CompareOperator.eq, value)
    key = None if resource_id is None else _as_key(mapping._id, resource_id)
    if key is not None:
        condition = and_(condition, mapping._id.expression != _bound(mapping._id, key))
    return select(mapping._id.expression).where(condition).limit(1)


def _links_statement(collection: _Collection, ids: list[str]) -> Select[Any]:
    """Return the statement loading the records a Link links to."""
    target = collection.target
    assert target is not None
    keys = [
        key for key in (_as_key(target._id, value) for value in ids) if key is not None
    ]
    return select(target.record).where(_among(target._id, keys))
