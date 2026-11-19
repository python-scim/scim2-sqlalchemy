Changelog
=========

[0.1.1] - 2026-20-21
--------------------

Changed
^^^^^^^
- Requires scim2-server 0.8.1.

Fixed
^^^^^
- The package imports without the ``asyncio`` extra.
- Per :rfc:`7643#section-2.1`, paths in a :class:`~scim2_sqlalchemy.ResourceMapping` are case
  insensitive. Two paths to the same attribute are refused, and so are an attribute and one of its
  sub-attributes.
- A single-valued attribute is refused when it is mapped to several
  :class:`~scim2_sqlalchemy.Link`.
- A storage refused for a missing mapping no longer leaves its mappings partly bound.

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
