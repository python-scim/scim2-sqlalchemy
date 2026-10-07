Changelog
=========

[0.1.0] - Unreleased
--------------------

Added
^^^^^
- :class:`~scim2_sqlalchemy.ResourceMapping`, which stores the resources of a SCIM model in the
  records of a SQLAlchemy model, with :class:`~scim2_sqlalchemy.Attribute`,
  :class:`~scim2_sqlalchemy.Many` and :class:`~scim2_sqlalchemy.Link`. A
  :class:`~scim2_sqlalchemy.Link` links to one resource, such as a ``manager``, or to
  several, such as ``members``.
- :class:`~scim2_sqlalchemy.SqlAlchemyStorage` and
  :class:`~scim2_sqlalchemy.AsyncSqlAlchemyStorage`, which serve the mappings to
  :doc:`scim2-server <scim2_server:index>`. They filter, sort and page the resources in the
  database, on one resource type or at the root of the server. The asynchronous storage needs
  the ``asyncio`` extra.
- :meth:`ResourceMapping.schemas <scim2_sqlalchemy.ResourceMapping.schemas>`, which publishes the
  attributes a mapping stores.
- Filters, sorts and uniqueness checks compare strings with the comparison key of the policy of
  the provider: exactly on SQLite, and approximately on PostgreSQL.
- A search or a read does not load the collections that ``attributes`` and ``excludedAttributes``
  remove from the response, such as the members of the groups.
