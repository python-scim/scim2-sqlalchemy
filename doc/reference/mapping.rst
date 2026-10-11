Mapping
=======

The classes describing how SCIM resources are stored in SQLAlchemy models, and the function
generating them from the resource types.

.. autoclass:: scim2_sqlalchemy.ResourceMapping
   :members: schemas

.. autoclass:: scim2_sqlalchemy.Attribute

.. autoclass:: scim2_sqlalchemy.Hashed

.. autoclass:: scim2_sqlalchemy.Many

.. autoclass:: scim2_sqlalchemy.Link

.. autofunction:: scim2_sqlalchemy.generate_mappings
