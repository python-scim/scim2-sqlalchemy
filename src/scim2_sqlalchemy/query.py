import operator
from collections.abc import Callable
from typing import Any

from scim2_models import AttributeBinding
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
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
from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import false
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import literal
from sqlalchemy import not_
from sqlalchemy import nulls_first
from sqlalchemy import nulls_last
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import true
from sqlalchemy.orm import Mapper

from .conversion import _as_stored
from .mapping import ResourceMapping
from .mapping import _Collection
from .mapping import _Column

_ORDERINGS: dict[CompareOperator, Callable[[Any, Any], Any]] = {
    CompareOperator.eq: operator.eq,
    CompareOperator.gt: operator.gt,
    CompareOperator.ge: operator.ge,
    CompareOperator.lt: operator.lt,
    CompareOperator.le: operator.le,
}

_SUBSTRINGS = {
    CompareOperator.co: "contains",
    CompareOperator.sw: "startswith",
    CompareOperator.ew: "endswith",
}

_ESCAPE = "/"


def _escape(value: str) -> str:
    """Escape the LIKE wildcards of a value, so that they match themselves."""
    for character in (_ESCAPE, "%", "_"):
        value = value.replace(character, _ESCAPE + character)
    return value


def _as_key(column: _Column, value: Any) -> Any:
    """Return an identifier in the type of its column, or None when it cannot have that type."""
    try:
        python_type = column.expression.type.python_type
    except NotImplementedError:
        return value
    try:
        return python_type(value)
    except (TypeError, ValueError):
        return None


def _unmapped(binding: AttributeBinding) -> InvalidFilterException:
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
        scope: tuple[AttrPath, _Collection] | None = None,
    ) -> None:
        self.mapping = mapping
        self.filter = scim_filter
        self.scope = scope

    def _path(self, attr_path: AttrPath) -> AttrPath:
        if self.scope is None:
            return attr_path
        scope_path, _ = self.scope
        return AttrPath(scope_path.attr, attr_path.attr, scope_path.uri)

    def _target(self, binding: AttributeBinding) -> tuple[_Collection | None, _Column]:
        collection, column = self.mapping._lookup(binding)
        if column is None or not column.readable:
            raise _unmapped(binding)
        return collection, column

    def _in_collection(
        self, collection: _Collection | None, condition: ColumnElement[bool]
    ) -> ColumnElement[bool]:
        if collection is None or self.scope is not None:
            return condition
        return collection.exists(condition)

    def visit_comparison(self, node: Comparison) -> ColumnElement[bool]:
        binding = self.filter.resolve_comparison(
            self._path(node.attr_path), strict=False
        )
        if binding is None:
            return false()
        collection, column = self._target(binding)
        value = coerce_value(binding, node.value, node.op)

        # 'emails ne "x"' holds when no email is "x", while
        # 'emails[value ne "x"]' holds when one email is not "x".
        if (
            node.op == CompareOperator.ne
            and collection is not None
            and self.scope is None
        ):
            equal = _compare(column, CompareOperator.eq, value)
            return not_(collection.exists(equal))
        return self._in_collection(collection, _compare(column, node.op, value))

    def visit_present(self, node: Present) -> ColumnElement[bool]:
        binding = self.filter.resolve(self._path(node.attr_path), strict=False)
        if binding is None:
            return false()
        collection, column = self.mapping._lookup(binding)
        if collection is not None and binding.sub_field_name is None:
            return collection.exists() if self.scope is None else true()
        if column is not None and column.readable:
            return self._in_collection(collection, _present(column))

        sub_columns = [
            column for column in self.mapping._sub_columns(binding) if column.readable
        ]
        if collection is not None or not sub_columns:
            raise _unmapped(binding)
        return or_(*(_present(column) for column in sub_columns))

    def visit_not(self, node: Not) -> ColumnElement[bool]:
        return not_(self.visit(node.expr))

    def visit_logical_expr(self, node: LogicalExpr) -> ColumnElement[bool]:
        combine = and_ if node.op == LogicalOperator.and_ else or_
        return combine(*(self.visit(term) for term in node.terms))

    def visit_value_path(self, node: ValuePath) -> ColumnElement[bool]:
        binding = self.filter.resolve(node.attr_path, strict=False)
        if binding is None:
            return false()
        collection, _ = self.mapping._lookup(binding)
        if collection is None:
            raise _unmapped(binding)
        scoped = _FilterTranslator(
            self.mapping, self.filter, (node.attr_path, collection)
        )
        return collection.exists(scoped.visit(node.val_filter))


def _present(column: _Column) -> ColumnElement[bool]:
    """Per RFC 7644 §3.4.2.2, an empty string is no value."""
    expression = column.expression
    if column.is_string:
        return and_(expression.is_not(None), expression != "")
    return expression.is_not(None)


def _compare(column: _Column, op: CompareOperator, value: Any) -> ColumnElement[bool]:
    """Compare a column with a value, following the case sensitivity of the attribute."""
    expression = column.compared
    value = _as_stored(value)
    if value is None:
        if op == CompareOperator.eq:
            return expression.is_(None)
        if op == CompareOperator.ne:
            return expression.is_not(None)
        return false()

    # Both sides are lowered by the database, so that a value always matches
    # itself, even where the database only lowers ASCII letters.
    def side(value: Any) -> Any:
        return func.lower(literal(value)) if column.casefolded else value

    left = func.lower(expression) if column.casefolded else expression
    if op == CompareOperator.ne:
        return or_(expression.is_(None), left != side(value))
    if op in _SUBSTRINGS:
        pattern = side(_escape(value))
        condition = getattr(left, _SUBSTRINGS[op])(pattern, escape=_ESCAPE)
    else:
        condition = _ORDERINGS[op](left, side(value))
    return and_(expression.is_not(None), condition)


def _order_by(
    mapping: ResourceMapping, search_request: SearchRequest[Any]
) -> list[Any]:
    """Return the ORDER BY terms of a search, the record identifier last.

    Without the identifier, records sharing a sort value could change pages
    from a request to the next.
    """
    terms: list[Any] = []
    binding = search_request.sort_binding(mapping.model)
    if binding is not None:
        collection, column = mapping._lookup(binding)
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
        if column.casefolded:
            expression = func.lower(expression)
        if collection is not None:
            expression = _first_entry(collection, expression)
        # Per RFC 7644 §3.4.2.3, resources without a value come last when
        # ascending and first when descending.
        if search_request.sort_order == SearchRequest.SortOrder.descending:
            terms.append(nulls_first(expression.desc()))
        else:
            terms.append(nulls_last(expression.asc()))
    terms.append(mapping._id.expression)
    return terms


def _first_entry(collection: _Collection, expression: Any) -> Any:
    """Return the value of the primary entry of a collection, or else of its first one."""
    relationship = collection.relationship_property
    entry_mapper: Mapper[Any] = inspect(collection.entry_class)
    order: list[Any] = []
    primary = collection.columns.get("primary")
    if primary is not None:
        order.append(nulls_last(primary.expression.desc()))
    order.extend(relationship.order_by or entry_mapper.primary_key)
    return (
        select(expression)
        .where(relationship.primaryjoin)
        .order_by(*order)
        .limit(1)
        .scalar_subquery()
    )


def _where(
    mapping: ResourceMapping, search_request: SearchRequest[Any]
) -> ColumnElement[bool]:
    if search_request.filter is None:
        return true()
    scim_filter = ScimFilter[mapping.model](str(search_request.filter))  # type: ignore[name-defined]
    return _FilterTranslator(mapping, scim_filter).visit(scim_filter.ast)


def _search_statements(
    mapping: ResourceMapping, search_request: SearchRequest[Any]
) -> tuple[Select[tuple[int]], Select[Any]]:
    """Return the statement counting the matching records, and the one selecting a page of them."""
    condition = _where(mapping, search_request)
    count = select(func.count()).select_from(mapping.record).where(condition)
    page = (
        select(mapping.record)
        .where(condition)
        .order_by(*_order_by(mapping, search_request))
        .options(*mapping._loader_options())
        .offset(search_request.start_index_0 or 0)
    )
    if search_request.count is not None:
        page = page.limit(search_request.count)
    return count, page


def _load_statement(
    mapping: ResourceMapping, resource_id: str | None
) -> Select[Any] | None:
    """Return the statement loading a record, or None when no record can have this identifier."""
    key = _as_key(mapping._id, resource_id)
    if key is None:
        return None
    return (
        select(mapping.record)
        .where(mapping._id.expression == key)
        .options(*mapping._loader_options())
        .execution_options(populate_existing=True)
    )


def _taken_statement(
    mapping: ResourceMapping, column: _Column, value: Any, resource_id: str | None
) -> Select[Any]:
    """Return the statement finding another record holding a unique value."""
    condition = _compare(column, CompareOperator.eq, value)
    key = None if resource_id is None else _as_key(mapping._id, resource_id)
    if key is not None:
        condition = and_(condition, mapping._id.expression != key)
    return select(mapping._id.expression).where(condition).limit(1)


def _links_statement(collection: _Collection, ids: list[str]) -> Select[Any]:
    """Return the statement loading the records a Link links to."""
    target = collection.target
    assert target is not None
    keys = [
        key for key in (_as_key(target._id, value) for value in ids) if key is not None
    ]
    return select(target.record).where(target._id.expression.in_(keys))
