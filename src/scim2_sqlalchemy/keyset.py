import decimal
from collections.abc import Sequence
from typing import Any

from scim2_models import InvalidCursorException
from scim2_server.keyset import Position
from sqlalchemy import BigInteger
from sqlalchemy import ColumnElement
from sqlalchemy import Integer
from sqlalchemy import and_
from sqlalchemy import false
from sqlalchemy import literal
from sqlalchemy import nulls_first
from sqlalchemy import nulls_last
from sqlalchemy import or_
from sqlalchemy import tuple_
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.visitors import InternalTraversal

from .mapping import _INTEGERS
from .mapping import _python_type


class _Sorted(ColumnElement[Any]):
    """A sort term on a value, placing the NULL values first or last."""

    inherit_cache = True
    _traverse_internals = [
        ("value", InternalTraversal.dp_clauseelement),
        ("ascending", InternalTraversal.dp_boolean),
        ("last", InternalTraversal.dp_boolean),
    ]

    def __init__(self, value: Any, ascending: bool, last: bool) -> None:
        self.value = value
        self.ascending = ascending
        self.last = last


@compiles(_Sorted)
def _compile_sorted(element: _Sorted, compiler: SQLCompiler, **kw: Any) -> str:
    """Write the sort term with NULLS FIRST or NULLS LAST."""
    term = element.value.asc() if element.ascending else element.value.desc()
    placed = nulls_last(term) if element.last else nulls_first(term)
    return compiler.process(placed, **kw)


@compiles(_Sorted, "mariadb")
@compiles(_Sorted, "mysql")
def _compile_sorted_mariadb(element: _Sorted, compiler: SQLCompiler, **kw: Any) -> str:
    """Write the sort term for MariaDB, which has no NULLS FIRST or NULLS LAST.

    MariaDB places NULL first when ascending and last when descending. A sort
    on whether the value is NULL comes first when it places NULL otherwise.
    """
    key = element.value
    term = compiler.process(key.asc() if element.ascending else key.desc(), **kw)
    if element.last != element.ascending:
        return term
    nulls = key.is_(None)
    placed = nulls.asc() if element.last else nulls.desc()
    return f"{compiler.process(placed, **kw)}, {term}"


def _key_order(key: Any, ascending: bool) -> Any:
    """Per RFC 7644 §3.4.2.3, resources without a value come last when ascending and first when descending."""
    return _Sorted(key, ascending, last=ascending)


def _keyset_order(
    key: Any, ties: Sequence[Any], descending: bool, forward: bool
) -> list[Any]:
    """Return the ORDER BY terms of a search: the sort value, then the ties.

    A previous page is read backward, in the reverse order of the sort.
    """
    terms = [] if key is None else [_key_order(key, descending != forward)]
    return [*terms, *(tie.asc() if forward else tie.desc() for tie in ties)]


def _beyond(
    key: Any,
    ties: Sequence[Any],
    anchor: Position,
    values: Sequence[Any],
    descending: bool,
) -> ColumnElement[bool]:
    """Return the condition keeping the rows past the anchor, in the reading direction.

    values are the ties of the anchor, in the types of the ties. The
    condition on the sort value is written out, as a row comparison would not
    place NULL where the sort does.
    """
    tie_after: ColumnElement[bool]
    if len(ties) == 1:
        tie_after = ties[0] > values[0] if anchor.forward else ties[0] < values[0]
    else:
        row, bound = tuple_(*ties), tuple_(*values)
        tie_after = row > bound if anchor.forward else row < bound
    if key is None:
        return tie_after

    ascending = descending != anchor.forward
    if anchor.key is None:
        key_after = key.is_not(None) if not ascending else false()
        return or_(key_after, and_(key.is_(None), tie_after))
    value = _sort_value(key, anchor.key)
    key_after = or_(key > value, key.is_(None)) if ascending else key < value
    return or_(key_after, and_(key == value, tie_after))


_ACCEPTED: dict[type, tuple[type, ...]] = {
    float: (float, int),
    decimal.Decimal: (decimal.Decimal, int),
}
"""The types of the sort values a key of decimal numbers accepts."""


def _sort_value(key: Any, value: Any) -> Any:
    """Return the sort value of an anchor, ready to compare with the sort key.

    A value of another type than the key is refused, such as a cursor given
    before the sort attribute moved to another column. An integer is a value
    of a key of decimal numbers, since a database may return a whole number
    as an integer. An integer no database stores is refused too. An integer
    compared with a key of integers is sent as a BIGINT, as filters send it.
    """
    python_type = _python_type(key)
    if python_type is not None and (
        not isinstance(value, _ACCEPTED.get(python_type, python_type))
        or (isinstance(value, bool) and python_type is not bool)
    ):
        raise InvalidCursorException
    if not isinstance(value, int) or isinstance(value, bool):
        return value
    if value not in _INTEGERS:
        raise InvalidCursorException
    if isinstance(key.type, Integer):
        return literal(value, BigInteger)
    return value
