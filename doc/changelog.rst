Changelog
=========

[0.1.0] - Unreleased
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
