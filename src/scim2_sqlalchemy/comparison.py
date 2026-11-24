import unicodedata
import warnings
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterable
from typing import Any

from scim2_models import AttributeBinding
from scim2_models import ScimPolicy
from scim2_models import default_comparison_key
from scim2_models.path import CompareOperator
from sqlalchemy import ColumnElement
from sqlalchemy import Dialect
from sqlalchemy import FunctionElement
from sqlalchemy import String
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import literal_column
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler

from .mapping import _MARIADB
from .mapping import ResourceMapping
from .mapping import _Column

_ESCAPE = "/"

_SUBSTRINGS = {
    CompareOperator.co: "contains",
    CompareOperator.sw: "startswith",
    CompareOperator.ew: "endswith",
}

_KEY_FUNCTION = "scim2_key"

_MARIADB_VERSION = (10, 10)
"""The first MariaDB version with the uca1400 collations."""


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


class _Formed(_Comparator, ABC):
    """Compare strings in a form the database computes on both sides.

    The value of a filter is normalized to NFC in Python first.
    """

    @abstractmethod
    def _form(self, column: _Column, expression: Any) -> ColumnElement[Any]:
        """Return a string expression in the form it is compared under."""

    def column(self, column: _Column) -> ColumnElement[Any]:
        """Return a string column in the form it is compared under."""
        return self._form(column, column.compared)

    def operand(self, column: _Column, value: str) -> Any:
        """Return a string normalized to NFC, in the form it is compared under."""
        return self._form(column, literal(unicodedata.normalize("NFC", value)))


class _Normalizer(_Formed):
    """Compare strings in PostgreSQL, normalized to NFC on both sides.

    The lower() of PostgreSQL follows the locale of the database, so the
    comparison approaches the default comparison key without reaching it. The
    C collation orders the strings by code point, as scim2-models does.
    """

    def _form(self, column: _Column, expression: Any) -> ColumnElement[Any]:
        """Normalize a string expression to NFC, lowered if the column ignores the case, under the C collation."""
        normalized = func.normalize(expression, literal_column("NFC"), type_=String)
        if column.casefolded:
            normalized = func.normalize(
                func.lower(normalized), literal_column("NFC"), type_=String
            )
        return normalized.collate("C")


class _Utf8mb4(FunctionElement[str]):
    """A string converted to utf8mb4, the character set of the MariaDB collations."""

    type = String()
    inherit_cache = True


@compiles(_Utf8mb4)
def _compile_utf8mb4(element: _Utf8mb4, compiler: SQLCompiler, **kw: Any) -> str:
    """Write the conversion of a string to utf8mb4."""
    (expression,) = element.clauses
    return f"CONVERT({compiler.process(expression, **kw)} USING utf8mb4)"


class _CodePoints(_Formed):
    """Compare strings in MariaDB by code point.

    The default collations ignore the case and the accents, and the trailing
    spaces. utf8mb4_nopad_bin compares and orders by code point, as
    scim2-models does. lower() only knows recent letters under a uca1400
    collation. MariaDB has no normalize(), so a stored value is compared in
    the form it was written in.
    """

    def _form(self, column: _Column, expression: Any) -> ColumnElement[Any]:
        """Return a string expression in utf8mb4, lowered if the column ignores the case, under a binary collation."""
        converted: ColumnElement[Any] = _Utf8mb4(expression)
        if column.casefolded:
            converted = func.lower(
                converted.collate("utf8mb4_uca1400_nopad_as_cs"), type_=String
            )
        return converted.collate("utf8mb4_nopad_bin")


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
        """Register the comparison key as a SQL function of a DBAPI connection."""
        connection.create_function(_KEY_FUNCTION, 2, self.key, deterministic=True)

    async def prepare_async(self, connection: Any) -> None:
        """Register the comparison key as a SQL function of an asyncio DBAPI connection."""
        await connection.create_function(_KEY_FUNCTION, 2, self.key, deterministic=True)

    def key(self, urn: str, value: Any) -> Any:
        """Return the comparison key of a stored value, or None if the key refuses it."""
        if value is None:
            return None
        try:
            return self.bindings[urn].comparable(value, self.policy)
        except ValueError:
            return None

    def column(self, column: _Column) -> ColumnElement[Any]:
        """Return the comparison key of a string column, computed by the SQL function."""
        key: ColumnElement[Any] = getattr(func, _KEY_FUNCTION)(
            literal(column.binding.urn), column.compared, type_=String
        )
        return key

    def operand(self, column: _Column, value: str) -> Any:
        """Return the comparison key of a string, computed in Python."""
        return column.binding.comparable(value, self.policy)

    def substring(
        self, op: CompareOperator, column: _Column, value: str
    ) -> ColumnElement[bool]:
        """Return whether a string column contains, starts or ends with a value, compared by their keys."""
        left = self.column(column)
        right = literal(self.operand(column, value))
        if op == CompareOperator.co:
            return func.instr(left, right) > 0
        if op == CompareOperator.sw:
            return func.substr(left, 1, func.length(right)) == right
        start = func.length(left) - func.length(right) + 1
        return func.substr(left, start) == right


def _comparator(
    dialect: Dialect, policy: ScimPolicy, mappings: Iterable[ResourceMapping]
) -> _Comparator:
    """Return how a database compares strings, warning when it cannot follow the policy.

    MySQL and MariaDB before 10.10 raise a ValueError.
    """
    if dialect.name == "sqlite":
        return _KeyFunction(policy, mappings)
    mariadb = dialect.name in _MARIADB
    if mariadb and not (
        getattr(dialect, "is_mariadb", False)
        and dialect.server_version_info is not None
        and dialect.server_version_info >= _MARIADB_VERSION
    ):
        raise ValueError(
            "The storage needs MariaDB 10.10 or later: MySQL and the older "
            "versions of MariaDB cannot compare strings by code point"
        )
    if policy.comparison_key is not default_comparison_key:
        warnings.warn(
            f"The {dialect.name} storage cannot compare strings with the comparison "
            "key of the policy: filters and uniqueness checks only approach it",
            stacklevel=2,
        )
    if dialect.name == "postgresql":
        return _Normalizer(policy)
    if mariadb:
        return _CodePoints(policy)
    return _Comparator(policy)
