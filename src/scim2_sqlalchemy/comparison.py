import unicodedata
import warnings
from collections.abc import Iterable
from typing import Any

from scim2_models import AttributeBinding
from scim2_models import ScimPolicy
from scim2_models import default_comparison_key
from scim2_models.path import CompareOperator
from sqlalchemy import ColumnElement
from sqlalchemy import String
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import literal_column

from .mapping import ResourceMapping
from .mapping import _Column

_ESCAPE = "/"

_SUBSTRINGS = {
    CompareOperator.co: "contains",
    CompareOperator.sw: "startswith",
    CompareOperator.ew: "endswith",
}

_KEY_FUNCTION = "scim2_key"


def _escape(value: str) -> str:
    """Escape the LIKE wildcards of a value, so that they match themselves."""
    for character in (_ESCAPE, "%", "_"):
        value = value.replace(character, _ESCAPE + character)
    return value


class _Comparator:
    """How a database compares the strings of a policy.

    The default lowers both sides with the lower() of the database, which
    approaches the default comparison key, and only lowers ASCII letters on
    some databases.
    """

    def __init__(self, policy: ScimPolicy) -> None:
        self.policy = policy

    def prepare(self, connection: Any) -> None:
        """Make a DBAPI connection ready to compare strings."""

    async def prepare_async(self, connection: Any) -> None:
        """Make an asyncio DBAPI connection ready to compare strings."""

    def column(self, column: _Column) -> ColumnElement[Any]:
        """Return a string column in the form it is compared under."""
        if column.casefolded:
            return func.lower(column.compared)
        return column.compared

    def operand(self, column: _Column, value: str) -> Any:
        """Return a string compared with a column, in the form it is compared under."""
        if column.casefolded:
            return func.lower(literal(value))
        return value

    def substring(
        self, op: CompareOperator, column: _Column, value: str
    ) -> ColumnElement[bool]:
        """Return whether a string column contains, starts or ends with a value."""
        pattern = self.operand(column, _escape(value))
        method = getattr(self.column(column), _SUBSTRINGS[op])
        return method(pattern, escape=_ESCAPE)  # type: ignore[no-any-return]


class _Normalizer(_Comparator):
    """Compare strings in PostgreSQL, normalized to NFC on both sides.

    The lower() of PostgreSQL follows the locale of the database, so the
    comparison approaches the default comparison key without reaching it. The
    C collation orders the strings by code point, as scim2-models does.
    """

    def _form(self, column: _Column, expression: Any) -> ColumnElement[Any]:
        normalized = func.normalize(expression, literal_column("NFC"), type_=String)
        if column.casefolded:
            normalized = func.normalize(
                func.lower(normalized), literal_column("NFC"), type_=String
            )
        return normalized.collate("C")

    def column(self, column: _Column) -> ColumnElement[Any]:
        return self._form(column, column.compared)

    def operand(self, column: _Column, value: str) -> Any:
        return self._form(column, literal(unicodedata.normalize("NFC", value)))


class _KeyFunction(_Comparator):
    """Compare strings in SQLite with the comparison key of the policy itself.

    The key is a SQL function of the connection, which takes the URN of the
    attribute and the value, and returns NULL for a value the key refuses.
    LIKE is not used, as SQLite ignores the case of ASCII letters in it.
    """

    def __init__(self, policy: ScimPolicy, mappings: Iterable[ResourceMapping]):
        super().__init__(policy)
        self.bindings: dict[str, AttributeBinding] = {
            column.binding.urn: column.binding
            for mapping in mappings
            for column in mapping._string_columns()
        }

    def prepare(self, connection: Any) -> None:
        connection.create_function(_KEY_FUNCTION, 2, self.key, deterministic=True)

    async def prepare_async(self, connection: Any) -> None:
        await connection.create_function(_KEY_FUNCTION, 2, self.key, deterministic=True)

    def key(self, urn: str, value: Any) -> Any:
        if value is None:
            return None
        try:
            return self.bindings[urn].comparable(value, self.policy)
        except ValueError:
            return None

    def column(self, column: _Column) -> ColumnElement[Any]:
        key: ColumnElement[Any] = getattr(func, _KEY_FUNCTION)(
            literal(column.binding.urn), column.compared, type_=String
        )
        return key

    def operand(self, column: _Column, value: str) -> Any:
        return column.binding.comparable(value, self.policy)

    def substring(
        self, op: CompareOperator, column: _Column, value: str
    ) -> ColumnElement[bool]:
        left = self.column(column)
        right = literal(self.operand(column, value))
        if op == CompareOperator.co:
            return func.instr(left, right) > 0
        if op == CompareOperator.sw:
            return func.substr(left, 1, func.length(right)) == right
        start = func.length(left) - func.length(right) + 1
        return func.substr(left, start) == right


def _comparator(
    dialect: str, policy: ScimPolicy, mappings: Iterable[ResourceMapping]
) -> _Comparator:
    """Return how a database compares strings, warning when it cannot follow the policy."""
    if dialect == "sqlite":
        return _KeyFunction(policy, mappings)
    if policy.comparison_key is not default_comparison_key:
        warnings.warn(
            f"The {dialect} storage cannot compare strings with the comparison key "
            "of the policy: filters and uniqueness checks only approach it",
            stacklevel=2,
        )
    if dialect == "postgresql":
        return _Normalizer(policy)
    return _Comparator(policy)
