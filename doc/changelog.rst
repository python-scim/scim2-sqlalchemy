Changelog
=========

[0.3.0] - 2026-11-26
--------------------

Added
^^^^^
- :meth:`~scim2_sqlalchemy.ResourceMapping.schemas` marks as ``required`` an attribute whose column
  cannot be ``NULL`` and has no default.

Changed
^^^^^^^
- scim2-server 0.8.9 and scim2-models 0.12.7 or later are required.
- The storage returns the values that clients never read back, write-only or never returned, in
  the form their column holds, such as a hash. It does not write again a value equal to the stored
  one. A PATCH that removes such a value, or a PUT that sets it to ``null``, clears it.
- :class:`~scim2_sqlalchemy.Attribute` no longer takes ``readable``: make the attribute
  ``writeOnly`` in the schema.
- A mapping that worked in 0.2.0 can now raise a :class:`ValueError` for an attribute that clients
  could neither read nor write, an ``immutable`` attribute on a :class:`~scim2_sqlalchemy.Hashed`
  column, a :class:`~scim2_sqlalchemy.Many` holding values the schema never returns, or a
  :class:`~scim2_sqlalchemy.Link` whose removal would write ``NULL`` in a foreign key that refuses
  it.
- A mapping warns about a column that needs a value the storage never writes.
- :func:`~scim2_sqlalchemy.generate_mappings` stores no value of a multi-valued attribute that the
  schema never returns.

Fixed
^^^^^
- An attribute that clients never read back, or a complex attribute none of whose sub-attributes
  is returned, is published with ``returned`` ``never``, as ``writeOnly`` unless it is
  ``immutable``, and never as ``required``.
- The storage neither filters, sorts, nor checks the uniqueness of a value that clients never read
  back, and a link never displays it.
- An attribute on a :class:`~sqlalchemy.schema.Computed` column is read-only.
- A change that a foreign key of the database refuses gets a 409, rather than a 500 error.

[0.2.0] - 2026-11-24
--------------------

Added
^^^^^
- MariaDB 10.10 and later is supported. Strings compare by code point, as on PostgreSQL. The
  storage raises a :class:`ValueError` on MySQL and on older versions of MariaDB.

Changed
^^^^^^^
- scim2-server 0.8.8 or later is required.
  A cursor that holds a sort value now raises ``invalidCursor`` on a search without ``sortBy``.

- A mapping that worked in 0.1.2 can now raise a :class:`ValueError`:

  - a :class:`~scim2_sqlalchemy.Many` cannot hold write-only values, and its sub-attributes are
    either all written or all read-only, since writing the resource replaces the entries;
  - a string attribute is mapped to a column of strings or of bytes, or to a property casting
    another column, except for ``id``;
  - ``id`` is mapped to a column that cannot hold ``NULL``, or to a property.

- A :class:`~scim2_sqlalchemy.SqlAlchemyStorage` now raises a :class:`ValueError` when its provider
  declares a resource type that no mapping stores.

- :func:`~scim2_sqlalchemy.generate_mappings` gives no column to a write-only sub-attribute of a
  multi-valued attribute.

Fixed
^^^^^
- The read-only entries of a :class:`~scim2_sqlalchemy.Many` are no longer written.
- A new resource, or an entry, without a value that its column needs is refused with a 400 error,
  rather than a 500. A column needs a value when it cannot be ``NULL`` and has no default.
- A string attribute in a column of another type no longer fails on each read: the mapping refuses
  it.
- A filter on a UUID the database stores without hyphens, as SQLite does, such as
  ``id sw "2819c223-"``, compares it in its form with hyphens.
- An identifier too large for its integer column finds nothing, rather than raising a 500 error.
  In a filter, an integer too large for its column compares as a number beyond every stored value.
  A cursor holding an integer no database stores is an invalid cursor, and so is a cursor whose
  sort value does not fit the sort column, rather than raising a 500 error.
- A search at the root sorted on an attribute that the resource types store in different types is
  refused with a 400 error, rather than a 500.
- An update only checks the unique values it changes, so a resource sharing a value with another
  one, under the case rules of SCIM, remains writable.
- A new resource carrying the ``id`` of another resource gets a 409 for its unique values, rather
  than a 500 error.
- A value that a unique constraint of the database refuses gets a 409, rather than a 500 error,
  even when the storage finds no other resource holding it.
- On PostgreSQL, a filter on a boolean stored in a column of integers no longer raises a 500
  error.

[0.1.2] - 2026-11-19
--------------------

Fixed
^^^^^
- Per :rfc:`7643#section-2.1`, paths in a :class:`~scim2_sqlalchemy.ResourceMapping` are case
  insensitive. Two paths to the same attribute are refused, and so are an attribute and one of its
  sub-attributes.
- A single-valued attribute is refused when it is mapped to several
  :class:`~scim2_sqlalchemy.Link`.
- A storage refused for a missing mapping no longer leaves its mappings partly bound.

[0.1.1] - 2026-10-21
--------------------

Changed
^^^^^^^
- Requires scim2-server 0.8.1.

Fixed
^^^^^
- The package imports without the ``asyncio`` extra.

[0.1.0] - 2026-10-11
--------------------

Added
^^^^^
- Initial release: :class:`~scim2_sqlalchemy.SqlAlchemyStorage` and
  :class:`~scim2_sqlalchemy.AsyncSqlAlchemyStorage` serve SQLAlchemy models to
  :doc:`scim2-server <scim2_server:index>`. They filter, sort and page the resources in the
  database, with ``startIndex`` or with the cursors of :rfc:`9865`.
- :class:`~scim2_sqlalchemy.ResourceMapping` maps the resources to existing SQLAlchemy models.
  :func:`~scim2_sqlalchemy.generate_mappings` generates the tables and the mappings for a server
  without a database of its own.
