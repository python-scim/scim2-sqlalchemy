Changelog
=========

[0.1.3] - Unreleased
--------------------

Changed
^^^^^^^
- A mapping that worked in 0.1.2 can now raise a :class:`ValueError`:

  - a :class:`~scim2_sqlalchemy.Many` cannot hold write-only values, and its sub-attributes are
    either all written or all read-only, since writing the resource replaces the entries;
  - a string attribute is mapped to a column of strings or of bytes, or to a property casting
    another column, except for ``id``.

- :func:`~scim2_sqlalchemy.generate_mappings` gives no column to a write-only sub-attribute of a
  multi-valued attribute.

Fixed
^^^^^
- The read-only entries of a :class:`~scim2_sqlalchemy.Many` are no longer written.
- An entry without a value that its column needs is refused with a 400 error, rather than a 500.
- A string attribute in a column of another type no longer fails on each read: the mapping refuses
  it.

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
