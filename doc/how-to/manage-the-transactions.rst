Manage the transactions
=======================

This guide commits the changes of each SCIM request, in a synchronous or an asynchronous
application. It is for developers who serve a mapping with scim2-server, as in the
:doc:`../overview`. It does not cover the integration in a web framework, which usually
manages the session and its commit: for it, read
:doc:`scim2_server:how-to/integrate-a-web-framework`.

The storage never commits. It :ref:`flushes <sqlalchemy:session_flushing>` each write, so that the database fills the identifiers
and checks its constraints, and leaves the transaction open. The application decides when the
changes of a request are committed.

Give the storage the current session
------------------------------------

Pass the storage a function returning the session of the current request. A
:class:`~sqlalchemy.orm.scoped_session` is such a function: calling it returns the session of
the current thread. For an asynchronous application, an
:class:`~sqlalchemy.ext.asyncio.async_scoped_session` returns the session of the current task:

.. tab-set::
   :class: outline

   .. tab-item:: Sync
      :sync: sync

      .. code-block:: python

         from sqlalchemy import create_engine
         from sqlalchemy.orm import scoped_session
         from sqlalchemy.orm import sessionmaker

         engine = create_engine("postgresql+psycopg://localhost/intranet")
         session = scoped_session(sessionmaker(engine))

   .. tab-item:: Async
      :sync: async

      Install the ``asyncio`` extra, which brings the asynchronous support of SQLAlchemy:

      .. code-block:: console

         $ pip install "scim2-sqlalchemy[asyncio]"

      .. code-block:: python

         import asyncio

         from sqlalchemy.ext.asyncio import async_scoped_session
         from sqlalchemy.ext.asyncio import async_sessionmaker
         from sqlalchemy.ext.asyncio import create_async_engine

         engine = create_async_engine("postgresql+psycopg://localhost/intranet")
         session = async_scoped_session(
             async_sessionmaker(engine), scopefunc=asyncio.current_task
         )

Commit at the end of each request
---------------------------------

Override the ``serve`` method of the application, which serves one request. Commit when the
response is not a server error, roll back otherwise, and release the session:

.. tab-set::
   :class: outline

   .. tab-item:: Sync
      :sync: sync

      .. literalinclude:: ../_examples/intranet/transactions.py
         :language: python
         :pyobject: IntranetApplication

   .. tab-item:: Async
      :sync: async

      .. literalinclude:: ../_examples/intranet/transactions.py
         :language: python
         :pyobject: AsyncIntranetApplication

A client error, such as a value already taken, leaves nothing to roll back: the storage writes
each operation in a :ref:`savepoint <sqlalchemy:session_begin_nested>`, and rolls the savepoint back when the operation fails.

Check that the changes are committed
------------------------------------

Send a request, then read the database with a session of its own. The following example uses a
SQLite database in a temporary directory:

.. doctest::

   >>> import tempfile
   >>> from sqlalchemy import create_engine
   >>> from sqlalchemy import func
   >>> from sqlalchemy import select
   >>> from sqlalchemy.orm import Session
   >>> from sqlalchemy.orm import scoped_session
   >>> from sqlalchemy.orm import sessionmaker
   >>> from werkzeug.test import Client
   >>> from intranet.models import Account
   >>> from intranet.models import Base
   >>> from intranet.scim import create_provider
   >>> from intranet.transactions import IntranetApplication

   >>> engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/intranet.sqlite")
   >>> Base.metadata.create_all(engine)
   >>> client = Client(IntranetApplication(scoped_session(sessionmaker(engine)), create_provider()))
   >>> response = client.post(
   ...     "/v2/Users",
   ...     json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "bjensen"},
   ... )
   >>> response.status_code
   201
   >>> with Session(engine) as other_session:
   ...     other_session.scalar(select(func.count()).select_from(Account))
   1

The asynchronous application commits the same way:

.. doctest::

   >>> import asyncio
   >>> from scim2_server.requests import ScimRequest
   >>> from sqlalchemy.ext.asyncio import async_scoped_session
   >>> from sqlalchemy.ext.asyncio import async_sessionmaker
   >>> from sqlalchemy.ext.asyncio import create_async_engine
   >>> from intranet.transactions import AsyncIntranetApplication

   >>> async def create_user():
   ...     async_engine = create_async_engine(str(engine.url).replace("sqlite:", "sqlite+aiosqlite:"))
   ...     session = async_scoped_session(async_sessionmaker(async_engine), scopefunc=asyncio.current_task)
   ...     application = AsyncIntranetApplication(session, create_provider())
   ...     request = ScimRequest(
   ...         "POST",
   ...         "https://intranet.example/v2",
   ...         "/Users",
   ...         headers={"Content-Type": "application/scim+json"},
   ...         body=b'{"userName": "jsmith"}',
   ...     )
   ...     response = await application.serve(request)
   ...     await async_engine.dispose()
   ...     return response.status
   >>> asyncio.run(create_user())
   <HTTPStatus.CREATED: 201>
   >>> with Session(engine) as other_session:
   ...     other_session.scalar(select(func.count()).select_from(Account))
   2
   >>> engine.dispose()

Write the operations of a bulk request
--------------------------------------

The handler of scim2-server encloses each operation of a bulk request in the ``operation``
context of the storage, which opens a savepoint. A failed operation is rolled back alone, and
the next operations go on, as :rfc:`RFC 7644 §3.7 <7644#section-3.7>` requires. The commit at the
end of the request keeps the operations that succeeded. For more on the bulk requests, read
:doc:`scim2_server:explanation/bulk`.
