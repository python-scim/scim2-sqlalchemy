Errors
======

The storages raise the following :class:`~scim2_models.SCIMException`, which scim2-server turns
into error responses:

.. list-table::
   :header-rows: 1

   * - Exception
     - Raised when
   * - :class:`~scim2_models.NotFoundException`
     - The resource does not exist, or its identifier cannot have the type of the key column.
   * - :class:`~scim2_models.PreconditionFailedException`
     - The stored version differs from ``expected_version``, or another transaction changed the
       record before the write.
   * - :class:`~scim2_models.UniquenessException`
     - Another resource holds a unique value of the resource, such as its ``userName``.
   * - :class:`~scim2_models.InvalidValueException`
     - The resource holds a value no mapping stores, an entry of a
       :class:`~scim2_sqlalchemy.Link` without ``value``, or a link to an unknown resource.
   * - :class:`~scim2_models.MutabilityException`
     - The resource changes an attribute the mapping only reads, or removes a value whose column
       cannot be ``NULL``.
   * - :class:`~scim2_models.InvalidFilterException`
     - The filter compares an attribute the mapping does not store, or a write-only attribute.
   * - :class:`~scim2_models.InvalidPathException`
     - The search sorts on an attribute the mapping does not store, or on a
       :class:`~scim2_sqlalchemy.Link`.

An integrity error of the database other than a unique value already taken goes through as a
:class:`sqlalchemy.exc.IntegrityError`.
