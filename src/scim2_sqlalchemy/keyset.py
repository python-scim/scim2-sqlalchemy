import datetime
import decimal
import uuid
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scim2_models import InvalidCursorException
from sqlalchemy import ColumnElement
from sqlalchemy import and_
from sqlalchemy import false
from sqlalchemy import nulls_first
from sqlalchemy import nulls_last
from sqlalchemy import or_
from sqlalchemy import tuple_

_TAGGED: dict[str, tuple[type, Callable[[Any], str], Callable[[str], Any]]] = {
    "datetime": (
        datetime.datetime,
        datetime.datetime.isoformat,
        datetime.datetime.fromisoformat,
    ),
    "date": (datetime.date, datetime.date.isoformat, datetime.date.fromisoformat),
    "decimal": (decimal.Decimal, str, decimal.Decimal),
    "uuid": (uuid.UUID, str, uuid.UUID),
}


def _encode(value: Any) -> Any:
    """Return a sort value as a value JSON can hold, tagged with its type when JSON has none."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    for tag, (python_type, dump, _) in _TAGGED.items():
        if isinstance(value, python_type):
            return {tag: dump(value)}
    raise TypeError(f"Cannot page on a sort value of type {type(value).__name__}")


def _decode(value: Any) -> Any:
    """Return the sort value a position holds."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if not isinstance(value, dict) or len(value) != 1:
        raise InvalidCursorException
    ((tag, text),) = value.items()
    if tag not in _TAGGED or not isinstance(text, str):
        raise InvalidCursorException
    try:
        return _TAGGED[tag][2](text)
    except (ValueError, decimal.InvalidOperation):
        raise InvalidCursorException from None


@dataclass(frozen=True)
class _Anchor:
    """The row a cursor page starts after, or ends before.

    ties are the values that order the rows sharing a sort value: the
    identifier as text, preceded at the root by the position of the resource
    type.
    """

    forward: bool
    key: Any
    ties: tuple[Any, ...]


def _anchor(position: Any, root: bool) -> _Anchor | None:
    """Read the position a storage gave, or return None for the first page."""
    if position is None:
        return None
    names = {"d", "k", "t", "i"} if root else {"d", "k", "i"}
    if not isinstance(position, dict) or set(position) != names:
        raise InvalidCursorException
    if position["d"] not in ("next", "previous") or not isinstance(position["i"], str):
        raise InvalidCursorException
    ties: tuple[Any, ...] = (position["i"],)
    if root:
        if type(position["t"]) is not int:
            raise InvalidCursorException
        ties = (position["t"], position["i"])
    return _Anchor(position["d"] == "next", _decode(position["k"]), ties)


def _position(direction: str, key: Any, ties: Sequence[Any]) -> Any:
    """Return the position of the page before or after a row, as a value JSON can hold.

    The sort value is null when the search has no sort.
    """
    position: dict[str, Any] = {"d": direction, "k": _encode(key)}
    if len(ties) == 2:
        position["t"] = ties[0]
    position["i"] = ties[-1]
    return position


def _key_order(key: Any, ascending: bool) -> Any:
    """Per RFC 7644 §3.4.2.3, resources without a value come last when ascending and first when descending."""
    if ascending:
        return nulls_last(key.asc())
    return nulls_first(key.desc())


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
    anchor: _Anchor,
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
    key_after = or_(key > anchor.key, key.is_(None)) if ascending else key < anchor.key
    return or_(key_after, and_(key == anchor.key, tie_after))
