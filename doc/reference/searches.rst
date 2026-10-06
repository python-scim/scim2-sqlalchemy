Searches
========

The storages filter, sort and page the resources in the database. The following table lists
what a search supports:

.. list-table::
   :header-rows: 1

   * - Feature
     - Support
   * - Comparison operators
     - ``eq``, ``ne``, ``co``, ``sw``, ``ew``, ``gt``, ``ge``, ``lt``, ``le``, and ``pr``.
   * - Logical operators
     - ``and``, ``or``, ``not``, and value selections such as ``emails[type eq "work"]``.
   * - Filtered attributes
     - Columns, :class:`hybrid properties <sqlalchemy.ext.hybrid.hybrid_property>`, the entries of a :class:`~scim2_sqlalchemy.Many`, and the
       ``value`` and the display name of a :class:`~scim2_sqlalchemy.Link`.
   * - Sorted attributes
     - Columns, hybrid properties, and the entries of a :class:`~scim2_sqlalchemy.Many`, on the
       ``primary`` entry or else the first one.
   * - Pagination
     - ``startIndex`` and ``count``. Cursors are not supported.
   * - Resource types
     - One per search. A search at the root raises
       :class:`~scim2_models.NotImplementedException`.

A string compares without its case, as the database lowers it, unless its attribute is
``caseExact``. A resource without a
value sorts last when ascending, and first when descending. An attribute the resource type does
not declare matches no resource.
