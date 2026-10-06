Overview
========

This tutorial builds a SCIM server over the tables of an existing application. The application
is an intranet that keeps accounts, their email addresses, and teams. At the end, the server
serves the accounts as SCIM users and the teams as SCIM groups, and passes the checks of
:doc:`scim2-tester <scim2_tester:index>`.

The tutorial is for Python developers whose application stores its data with the
:doc:`SQLAlchemy ORM <sqlalchemy:orm/index>`, version 2.0. It assumes familiarity with SQLAlchemy
models and sessions, and with the users and groups of SCIM (:rfc:`RFC 7643 §4 <7643#section-4>`).
It runs the server without a web framework. To serve it from one, read
:doc:`scim2_server:how-to/integrate-a-web-framework`. To commit its changes, read
:doc:`how-to/manage-the-transactions`.

Install scim2-sqlalchemy. It brings SQLAlchemy, :doc:`scim2-server <scim2_server:index>` and
:doc:`scim2-models <scim2_models:index>` along:

.. code-block:: console

   $ pip install scim2-sqlalchemy

Start from the tables
---------------------

The intranet already has its SQLAlchemy models. scim2-sqlalchemy uses them as they are. An
account has a login, a name, a password hash, email addresses and teams:

.. literalinclude:: _examples/intranet/models.py
   :language: python
   :pyobject: Account

The models need three things for SCIM:

- a column for the identifier the identity provider gives, here ``external_id``;
- a creation date and a modification date, here ``created_at`` and ``updated_at``;
- a :ref:`version counter <sqlalchemy:mapper_version_counter>`, declared as the
  ``version_id_col`` of the mapper, here ``revision``.
  SQLAlchemy increments it on every write, and SCIM clients send it back in their
  ``If-Match`` headers.

The email addresses and the teams live in tables of their own:

.. literalinclude:: _examples/intranet/models.py
   :language: python
   :pyobject: EmailAddress

.. literalinclude:: _examples/intranet/models.py
   :language: python
   :pyobject: Team

Map the accounts to users
-------------------------

A :class:`~scim2_sqlalchemy.ResourceMapping` stores a SCIM model in a SQLAlchemy model. It maps
the path of each SCIM attribute to a column. The following mapping stores the
:class:`~scim2_models.User` resources in the ``accounts`` table:

.. literalinclude:: _examples/intranet/scim.py
   :language: python
   :start-at: USERS =
   :end-before: GROUPS =

Each value of the mapping says how the attribute is stored:

- ``externalId``, ``userName``, ``name.givenName`` and ``active`` are plain columns. ``externalId`` holds
  the identifier the identity provider gives to the user.
- ``displayName`` is a :class:`~sqlalchemy.ext.hybrid.hybrid_property` without setter: the storage reads it, filters and
  sorts on it, and never writes it.
- ``password`` is a property whose setter hashes the password. The
  :class:`~scim2_sqlalchemy.Attribute` around it makes it write-only: the storage writes it,
  and never reads it back.
- ``emails`` is a :class:`~scim2_sqlalchemy.Many`: the email addresses belong to the account,
  and writing the user replaces them.
- ``groups`` is a :class:`~scim2_sqlalchemy.Link`: the teams are other resources, of the
  ``Group`` resource type.
- ``meta.created`` and ``meta.lastModified`` are the dates the storage fills.

The mapping checks every path against the :class:`~scim2_models.User` model when the module is
imported. A misspelled path raises a :class:`ValueError` at once.

Map the teams to groups
-----------------------

The teams become :class:`~scim2_models.Group` resources. Their members link back to the users:

.. literalinclude:: _examples/intranet/scim.py
   :language: python
   :start-at: GROUPS =
   :end-before: def create_provider

Describe the service
--------------------

A :class:`~scim2_models.ScimProvider` describes the service: the schemas of its resources, its
resource types, and the features it supports. Build it from the mappings: the schemas of each
mapping, and a resource type built from the model of each mapping, which lists the extensions
the mapping stores.
:meth:`~scim2_sqlalchemy.ResourceMapping.schemas` restricts each schema to the attributes the
mapping stores, so that clients only send what the server keeps:

.. literalinclude:: _examples/intranet/scim.py
   :language: python
   :pyobject: create_provider

Create the storage
------------------

A :class:`~scim2_sqlalchemy.SqlAlchemyStorage` reads and writes the resources with a SQLAlchemy
:class:`~sqlalchemy.orm.Session`. It receives the mapping of each resource type, a function returning the current
session, and the provider:

.. literalinclude:: _examples/intranet/scim.py
   :language: python
   :pyobject: create_storage

Create the tables in an in-memory SQLite database, and open a session:

.. doctest::

   >>> from sqlalchemy import create_engine
   >>> from sqlalchemy.orm import Session
   >>> from intranet.models import Base
   >>> engine = create_engine("sqlite://")
   >>> Base.metadata.create_all(engine)
   >>> session = Session(engine)

Serve the storage with the WSGI application of scim2-server, which handles the SCIM protocol:

.. doctest::

   >>> from scim2_server.applications.wsgi import WSGIApplication
   >>> from intranet.scim import create_provider
   >>> from intranet.scim import create_storage
   >>> provider = create_provider()
   >>> app = WSGIApplication(create_storage(session, provider), provider)

Serve requests
--------------

The application now answers SCIM requests. The examples send them with the test client of
Werkzeug, which calls the application without running a server. Create a user:

.. doctest::

   >>> from werkzeug.test import Client
   >>> client = Client(app)
   >>> USER = "urn:ietf:params:scim:schemas:core:2.0:User"
   >>> response = client.post(
   ...     "/v2/Users",
   ...     json={
   ...         "schemas": [USER],
   ...         "userName": "bjensen",
   ...         "name": {"givenName": "Barbara", "familyName": "Jensen"},
   ...         "emails": [{"value": "bjensen@example.com", "type": "work", "primary": True}],
   ...         "password": "t1meMa$heen",
   ...     },
   ... )
   >>> response.status_code
   201
   >>> user = response.json
   >>> user["id"], user["displayName"], user["active"], "password" in user
   ('1', 'Barbara Jensen', True, False)

The response comes from the new row of the ``accounts`` table. ``displayName`` comes from the
hybrid property, and ``active`` from the default of the ``active`` column. The password is
stored hashed:

.. doctest::

   >>> from intranet.models import Account
   >>> session.get(Account, 1).password_hash.count(":")
   1

Create a group with this user as a member. The member links to the user, with its URL:

.. doctest::

   >>> GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
   >>> response = client.post(
   ...     "/v2/Groups",
   ...     json={"schemas": [GROUP], "displayName": "Admins", "members": [{"value": "1"}]},
   ... )
   >>> response.json["members"]
   [{'value': '1', '$ref': 'http://localhost/v2/Users/1', 'display': 'Barbara Jensen'}]

Search the users. The storage turns the filter and the sort into a SQL query, so the database
only returns the matching rows:

.. doctest::

   >>> response = client.get(
   ...     "/v2/Users",
   ...     query_string={"filter": 'emails[type eq "work"] and groups pr', "sortBy": "userName"},
   ... )
   >>> response.json["totalResults"], response.json["Resources"][0]["userName"]
   (1, 'bjensen')

An attribute the server does not store is refused, rather than lost:

.. doctest::

   >>> response = client.post(
   ...     "/v2/Users",
   ...     json={"schemas": [USER], "userName": "jsmith", "phoneNumbers": [{"value": "555-1234"}]},
   ... )
   >>> response.status_code, response.json["detail"]
   (400, 'Extra inputs are not permitted: phoneNumbers')

Check the server
----------------

:doc:`scim2-tester <scim2_tester:index>` checks that a SCIM server complies with the RFCs. It
sends the requests an identity provider would send, and checks each response. Install it with
the Werkzeug engine of :doc:`scim2-client <scim2_client:index>`, which calls the application
directly:

.. code-block:: console

   $ pip install scim2-tester "scim2-client[werkzeug]"

Run the checks:

.. doctest::

   >>> from scim2_client.engines.werkzeug import TestSCIMClient
   >>> from scim2_tester import Status
   >>> from scim2_tester import check_server
   >>> scim_client = TestSCIMClient(client, scim_prefix="/v2", provider=provider)
   >>> results = check_server(scim_client)
   >>> [result.title for result in results if result.status not in (Status.SUCCESS, Status.SKIPPED)]
   []

Close the session and the engine when the application stops:

.. doctest::

   >>> session.close()
   >>> engine.dispose()

Next steps
----------

The server now serves the accounts and the teams of the intranet:

- To map other cases, such as extensions, custom resources and integer identifiers, read
  :doc:`how-to/map-a-data-model`.
- To choose between the restricted schemas and models written by hand, read
  :doc:`how-to/publish-the-stored-attributes`.
- To commit the changes of each request, in a synchronous or an asynchronous application, read
  :doc:`how-to/manage-the-transactions`.
- For the reasons behind the behavior of the storage, read :doc:`explanation`.
