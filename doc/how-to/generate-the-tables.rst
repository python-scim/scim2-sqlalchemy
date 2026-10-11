Generate the tables
===================

This guide generates the tables and the mappings of a SCIM server from its resource types. It is
for developers who start a server without a database of their own, such as a test server or an
application whose data model is SCIM. It assumes a :class:`~scim2_models.ScimProvider`
describing the resource types. To serve the tables of an existing application instead, follow
the :doc:`../overview` and :doc:`map-a-data-model`.

Generate the mappings
---------------------

Call :func:`~scim2_sqlalchemy.generate_mappings` with the provider and a
:class:`~sqlalchemy.schema.MetaData`. It adds the tables to the metadata, and returns a
:class:`~scim2_sqlalchemy.ResourceMapping` for each resource type, by name:

.. doctest::

   >>> import hashlib
   >>> import os
   >>> from sqlalchemy import MetaData
   >>> from scim2_server.utils import load_default_provider
   >>> from scim2_sqlalchemy import Hashed
   >>> from scim2_sqlalchemy import generate_mappings

   >>> def hash_password(password):
   ...     salt = os.urandom(16)
   ...     digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
   ...     return f"{salt.hex()}:{digest.hex()}"

   >>> PASSWORD = "urn:ietf:params:scim:schemas:core:2.0:User:password"
   >>> def store(binding):
   ...     return Hashed(hash_password) if binding.urn == PASSWORD else True

   >>> provider = load_default_provider()
   >>> metadata = MetaData()
   >>> mappings = generate_mappings(provider, metadata, store=store)
   >>> sorted(mappings)
   ['Group', 'User']

Build the provider from the models of the application, or from the schemas and the resource
types of a configuration file:

- ``ScimProvider(models=[User, EnterpriseUser, Group], resource_types=[...])``;
- :meth:`ScimProvider.from_discovery <scim2_models.ScimProvider.from_discovery>`.

Choose what is stored
---------------------

Pass ``store``, a function deciding how each attribute is kept. It receives the
:class:`~scim2_models.AttributeBinding` of the attribute, with its URN, and returns:

- :data:`True`, to store the attribute with the column type matching it;
- :data:`False`, not to store it: the published schemas leave it out;
- a column type, to store the attribute in a column of this type. Only an attribute with a
  column of its own takes one: a simple attribute, a sub-attribute, or the values of a
  multi-valued simple attribute.

Without ``store``, every attribute is stored with the column type matching it.

A write-only string, such as ``password``, is never stored in clear text: ``store`` gives it a
column type that hashes it, such as :class:`~scim2_sqlalchemy.Hashed`, or does not store it.
Otherwise, :func:`~scim2_sqlalchemy.generate_mappings` raises a :class:`ValueError`. The
application checks a password against the stored hash with the same hash function, so the choice
of the algorithm belongs to the application.

A server loading its schemas at run time does not know their URNs in advance. It can decide on
the annotations of the attribute instead, here to hash every write-only string:

.. code-block:: python

   from scim2_models import Mutability

   def store(binding):
       if binding.get_annotation(Mutability) == Mutability.write_only:
           return Hashed(hash_password)
       return True

A column type can also convert the values both ways, such as a
:class:`~sqlalchemy.types.TypeDecorator` encrypting them. Filters then compare the converted
values: ``eq`` works when the conversion always gives the same result, but ``sw``, ``co`` and the
sorts do not. A column type converting the values one way only, as a hash does, suits a
write-only attribute only, since the storage would return the converted values.

Create the tables
-----------------

Create the tables with :meth:`MetaData.create_all <sqlalchemy.schema.MetaData.create_all>`, or
with a migration tool such as Alembic. The names of the tables and of the columns only depend on
the schemas, so the migration tool finds the same names from a run to the next:

.. doctest::

   >>> from sqlalchemy import create_engine

   >>> engine = create_engine("sqlite://")
   >>> metadata.create_all(engine)
   >>> sorted(metadata.tables)[:4]
   ['groups', 'groups_members_groups', 'groups_members_users', 'users']

Pass ``prefix`` to start the name of every table with it, such as ``prefix="scim_"``, so that
the tables of SCIM stay apart from the other tables of the database.

Serve the resource types
------------------------

Pass the mappings to the storage. Build the provider of the server from the schemas of the
mappings, so that it publishes the attributes the tables store, as
:doc:`publish-the-stored-attributes` explains:

.. doctest::

   >>> from scim2_models import ScimProvider
   >>> from scim2_server.applications.wsgi import WSGIApplication
   >>> from scim2_server.utils import load_default_service_provider_config
   >>> from sqlalchemy.orm import Session
   >>> from werkzeug.test import Client
   >>> from scim2_sqlalchemy import SqlAlchemyStorage

   >>> published = ScimProvider.from_discovery(
   ...     [schema for mapping in mappings.values() for schema in mapping.schemas()],
   ...     provider.resource_types,
   ...     config=load_default_service_provider_config(),
   ... )
   >>> session = Session(engine)
   >>> storage = SqlAlchemyStorage(mappings, lambda: session, provider=published)
   >>> client = Client(WSGIApplication(storage, published))
   >>> user = client.post(
   ...     "/v2/Users", json={"userName": "bjensen", "password": "correct horse"}
   ... ).json
   >>> group = client.post(
   ...     "/v2/Groups", json={"displayName": "admins", "members": [{"value": user["id"]}]}
   ... ).json
   >>> client.get(f"/v2/Users/{user['id']}").json["groups"]
   [{'value': '...', '$ref': 'http://localhost/v2/Groups/...', 'display': 'admins', 'type': 'direct'}]
   >>> session.close()
   >>> engine.dispose()

Read the inverse of an attribute
--------------------------------

A read-only attribute is not stored, except an inverse: it reads the links of another attribute.
The ``groups`` of a user reads the ``members`` of the groups. Declare the other inverses of the
resource types with ``inverses``, by the URN of each attribute:

.. code-block:: python

   generate_mappings(
       provider,
       metadata,
       store=store,
       inverses={
           "urn:example:params:scim:schemas:extension:staffing:2.0:User:projects": (
               "urn:example:params:scim:schemas:core:2.0:Project:members"
           ),
       },
   )

The inverse links to the resource type of the attribute it reverses, which links to the resource
type of the inverse. Both are multi-valued.

For the tables and columns each kind of attribute gets, and for the attributes left out, read
:func:`~scim2_sqlalchemy.generate_mappings`.
