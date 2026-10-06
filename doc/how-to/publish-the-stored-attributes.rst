Publish the stored attributes
=============================

This guide makes the server announce the attributes its storage keeps, so that clients do not
send values the server would lose. It is for developers who serve a mapping with scim2-server,
as in the :doc:`../overview`. It does not cover the mapping itself: for it, read
:doc:`map-a-data-model`.

A SCIM server publishes the schemas of its resources on ``/Schemas``, from the models of its
:class:`~scim2_models.ScimProvider`. A mapping rarely stores every attribute of the standard
schemas: the intranet stores no ``phoneNumbers``. Choose one of the following approaches.

The examples use the intranet of the :doc:`../overview`:

.. doctest::

   >>> from scim2_models import Group
   >>> from scim2_models import ResourceType
   >>> from scim2_models import ScimPolicy
   >>> from scim2_models import ScimProvider
   >>> from scim2_models import User
   >>> from scim2_server.applications.wsgi import WSGIApplication
   >>> from scim2_server.utils import load_default_service_provider_config
   >>> from sqlalchemy import create_engine
   >>> from sqlalchemy.orm import Session
   >>> from werkzeug.test import Client
   >>> from intranet.models import Base
   >>> from intranet.scim import GROUPS
   >>> from intranet.scim import USERS
   >>> from scim2_sqlalchemy import SqlAlchemyStorage

   >>> engine = create_engine("sqlite://")
   >>> Base.metadata.create_all(engine)
   >>> session = Session(engine)
   >>> MAPPINGS = {"User": USERS, "Group": GROUPS}
   >>> USER = "urn:ietf:params:scim:schemas:core:2.0:User"

Publish the schemas of the mappings
-----------------------------------

Build the provider from :meth:`~scim2_sqlalchemy.ResourceMapping.schemas`. Each schema then
holds the mapped attributes only, and an attribute the mapping only reads is ``readOnly``:

.. doctest::

   >>> provider = ScimProvider.from_discovery(
   ...     [*USERS.schemas(), *GROUPS.schemas()],
   ...     [ResourceType.from_resource(USERS.model), ResourceType.from_resource(GROUPS.model)],
   ...     config=load_default_service_provider_config(),
   ... )
   >>> [(attribute.name, attribute.mutability.value) for attribute in USERS.schemas()[0].attributes]
   [('userName', 'readWrite'), ('name', 'readWrite'), ('displayName', 'readOnly'), ('active', 'readWrite'), ('password', 'writeOnly'), ('emails', 'readWrite'), ('groups', 'readOnly')]

Pass the provider to the storage too. The storage then returns resources of the models of the
provider, and gives the links a ``$ref``:

.. doctest::

   >>> storage = SqlAlchemyStorage(MAPPINGS, lambda: session, provider=provider)
   >>> client = Client(WSGIApplication(storage, provider))

The server refuses a request with an attribute the schemas do not hold, before it reaches the
storage:

.. doctest::

   >>> response = client.post(
   ...     "/v2/Users",
   ...     json={"schemas": [USER], "userName": "bjensen", "phoneNumbers": [{"value": "555-1234"}]},
   ... )
   >>> response.status_code, response.json["scimType"]
   (400, 'invalidSyntax')

The server ignores a value for a read-only attribute, as :rfc:`RFC 7644 §3.5.1
<7644#section-3.5.1>` requires.

``externalId`` is an attribute of every resource (:rfc:`RFC 7643 §3.1 <7643#section-3.1>`), not
of a schema, so the published models always hold it. Map it, as the intranet does: identity
providers set it on every resource they create.

Ignore the attributes the server does not store
-----------------------------------------------

Some clients send every attribute they know, whatever the schemas say. To accept their requests,
build the provider with a :class:`~scim2_models.ScimPolicy` that ignores the unknown
attributes:

.. doctest::

   >>> tolerant_provider = ScimProvider.from_discovery(
   ...     [*USERS.schemas(), *GROUPS.schemas()],
   ...     [ResourceType.from_resource(USERS.model), ResourceType.from_resource(GROUPS.model)],
   ...     config=load_default_service_provider_config(),
   ...     policy=ScimPolicy(unknown=ScimPolicy.Unknown.ignore),
   ... )
   >>> storage = SqlAlchemyStorage(MAPPINGS, lambda: session, provider=tolerant_provider)
   >>> client = Client(WSGIApplication(storage, tolerant_provider))
   >>> response = client.post(
   ...     "/v2/Users",
   ...     json={"schemas": [USER], "userName": "bjensen", "phoneNumbers": [{"value": "555-1234"}]},
   ... )
   >>> response.status_code, "phoneNumbers" in response.json
   (201, False)

For the other tolerances of the policy, read
:doc:`scim2_models:how-to/tolerate-a-nonconformant-peer`.

Publish models written by hand
------------------------------

Write the SCIM models of the application instead when the published schemas need more than the
mapped attributes, such as descriptions. To write them, read
:doc:`scim2_models:how-to/define-custom-models`. Declare the models in the provider, and map the
same models.

The storage then checks every write against the mapping. It refuses a value the mapping cannot
store with a 400 error, rather than losing it. The following provider publishes the standard
:class:`~scim2_models.User`, which declares ``phoneNumbers``:

.. doctest::

   >>> provider = ScimProvider(
   ...     models=[User, Group], config=load_default_service_provider_config()
   ... )
   >>> storage = SqlAlchemyStorage(MAPPINGS, lambda: session, provider=provider)
   >>> client = Client(WSGIApplication(storage, provider))
   >>> response = client.post(
   ...     "/v2/Users",
   ...     json={"schemas": [USER], "userName": "jsmith", "phoneNumbers": [{"value": "555-1234"}]},
   ... )
   >>> response.status_code, response.json["scimType"]
   (400, 'invalidValue')
   >>> response.json["detail"]
   "'urn:ietf:params:scim:schemas:core:2.0:User:phoneNumbers.value' is not stored by this server"

A value for an attribute the mapping only reads gets a ``mutability`` error:

.. doctest::

   >>> response = client.post(
   ...     "/v2/Users",
   ...     json={"schemas": [USER], "userName": "jsmith", "displayName": "John Smith"},
   ... )
   >>> response.status_code, response.json["scimType"]
   (400, 'mutability')
   >>> session.close()
   >>> engine.dispose()

A value equal to the stored one is accepted, so a client can always send back a resource it read.
For every error the storage raises, read :doc:`../reference/errors`.
