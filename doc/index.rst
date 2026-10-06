scim2-sqlalchemy
================

scim2-sqlalchemy stores SCIM resources in the tables of an application, with the
:doc:`SQLAlchemy ORM <sqlalchemy:orm/index>`. A mapping declares which column holds each SCIM
attribute. A storage then reads, writes, filters, sorts and pages the resources for
:doc:`scim2-server <scim2_server:index>`, which serves the SCIM protocol of :rfc:`RFC 7643 <7643>`
and :rfc:`RFC 7644 <7644>`.

This documentation is for Python developers who add a SCIM server to an application whose data
lives in SQLAlchemy models. It assumes familiarity with SQLAlchemy 2.0. The protocol, the web
frameworks and the authentication are described in the
:doc:`scim2-server documentation <scim2_server:index>`.

.. code-block:: python

   users = ResourceMapping(
       User,
       Account,
       {
           "id": Account.id,
           "userName": Account.login,
           "name.givenName": Account.first_name,
           "emails": Many(Account.emails, {"value": EmailAddress.address}),
           "meta.created": Account.created_at,
           "meta.lastModified": Account.updated_at,
       },
       version=Account.revision,
   )
   storage = SqlAlchemyStorage({"User": users}, lambda: session)

.. code-block:: console

   $ pip install scim2-sqlalchemy

Choose a path
-------------

Start with the page that matches the need:

- To build a SCIM server over the tables of an existing application, follow the
  :doc:`overview`.
- To map other data models, publish the stored attributes, or commit the changes of each
  request, read the :doc:`how-to guides <how-to/index>`.
- For the reasons behind the behavior of the storage, read :doc:`explanation`.
- For the public API, the supported searches and the errors, read the :doc:`reference <reference/index>`.

.. toctree::
    :maxdepth: 2
    :hidden:

    Overview <overview>
    How-to guides <how-to/index>
    Explanation <explanation>
    Reference <reference/index>
    Contributing <contributing>
    Changelog <changelog>
