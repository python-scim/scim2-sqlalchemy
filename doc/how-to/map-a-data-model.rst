Map a data model
================

This guide maps the SQLAlchemy models of an application to SCIM resources, case by case. It is
for developers who have followed the :doc:`../overview` and map a model that it does not cover:
an extension, a computed value, a custom resource type, or tables with other identifiers. It
does not cover the choice of the schemas the server publishes: for this choice, read
:doc:`publish-the-stored-attributes`.

The examples extend the intranet of the :doc:`../overview`.

Prepare the SQLAlchemy model
----------------------------

Give each SQLAlchemy model the columns the storage needs:

- a primary key, mapped to ``id``;
- a creation date and a modification date, mapped to ``meta.created`` and
  ``meta.lastModified``;
- an integer :ref:`version counter <sqlalchemy:mapper_version_counter>`, declared as the
  ``version_id_col`` of the mapper, and passed as the ``version`` of the mapping.

A :class:`~scim2_sqlalchemy.ResourceMapping` without one of them raises a :class:`ValueError`.
The project model of the intranet has these columns:

.. literalinclude:: ../_examples/intranet/projects.py
   :language: python
   :pyobject: ProjectRecord

Per :rfc:`RFC 7643 §3.1 <7643#section-3.1>`, two resources never share an ``id``, even of
different types. A search at the root of the server returns the resources of every type
together, and clients tell them apart by their ``id``. The integers of an autoincrement column
start again in each table, so they break this rule as soon as the server serves two resource
types. Use a UUID, with the :class:`~sqlalchemy.types.Uuid` type, or a sequence that every
table shares.

The primary key may be an integer, a string or a UUID, and the storage returns it as a string.
The storage does not fill the key: give its column a default, such as ``default=uuid.uuid4``, a
server default, or an autoincrement. The storage reads the key the database gives.

Map a column
------------

Map an attribute to a column of the model. Map a complex attribute holding a single value, such
as ``name``, by its sub-attributes:

.. code-block:: python

   {
       "userName": Account.login,
       "name.givenName": Account.first_name,
       "name.familyName": Account.last_name,
   }

A path to a complex attribute alone, such as ``name``, raises a :class:`ValueError`. Leave out
the attributes the model has no column for: the storage refuses a value for them. To keep
clients from sending such values, read :doc:`publish-the-stored-attributes`.

Map an extension
----------------

Map the attribute of an extension by its full path, the URN of the schema then the attribute
name. The mapping takes the model with its extensions, such as ``User[Workplace]``:

.. literalinclude:: ../_examples/intranet/projects.py
   :language: python
   :start-at: class Workplace
   :end-before: project_members =

Map a computed value
--------------------

Map an attribute to a :class:`~sqlalchemy.ext.hybrid.hybrid_property` when the model computes
its value. The storage reads the property, and filters and sorts with its expression. Without a
setter, the attribute is read-only: the storage never writes it, and refuses a different value
with a 400 error. The ``displayName`` of the intranet joins the first and the last name:

.. literalinclude:: ../_examples/intranet/models.py
   :language: python
   :start-at: @hybrid_property
   :end-before: @hybrid_property
   :dedent: 4

Convert a value
---------------

Map an attribute to a :class:`~sqlalchemy.ext.hybrid.hybrid_property` with a setter and an
expression when the column holds the value in another form. The getter and the setter convert
the value in Python. The expression converts it in SQL, so that filters and sorts compare SCIM
values. The following model stores ``active`` in a ``status`` column, which holds ``enabled``
or ``disabled``:

.. code-block:: python

   class Account(Base):
       status: Mapped[str | None]

       @hybrid_property
       def active(self):
           return None if self.status is None else self.status == "enabled"

       @active.inplace.setter
       def _active_setter(self, value):
           self.status = None if value is None else ("enabled" if value else "disabled")

       @active.inplace.expression
       @classmethod
       def _active_expression(cls):
           return cls.status == "enabled"

Map the attribute to the property, with ``"active": Account.active``. The filter
``active eq true`` then returns the accounts whose status is ``enabled``.

Keep the expression ``NULL`` when the column is ``NULL``, as ``status == "enabled"`` does. The
filter ``active pr`` then holds only for the accounts with a status. A request removing the
attribute passes :data:`None` to the setter.

Map a write-only or a read-only attribute
-----------------------------------------

Wrap a column or a property in an :class:`~scim2_sqlalchemy.Attribute` to limit what the
storage does with it:

- with ``readable=False``, the attribute is write-only: the storage never returns it, nor
  filters or sorts on it.
- with ``writable=False``, the attribute is read-only, even when its column or its property
  can be written.

The intranet hashes the password in the setter of a property:

.. literalinclude:: ../_examples/intranet/models.py
   :language: python
   :start-at: def password(self)
   :end-before: class EmailAddress
   :dedent: 4
   :prepend: @hybrid_property

The mapping makes the password write-only:

.. code-block:: python

   {"password": Attribute(Account.password, readable=False)}

A write-only attribute keeps its stored value when a request does not carry it, since a
resource never carries it back.

Map entries of a child table
----------------------------

Map a multi-valued attribute whose entries belong to the resource, such as ``emails``, with
:class:`~scim2_sqlalchemy.Many`. Give it the :func:`~sqlalchemy.orm.relationship` to the child model, and the column of
each sub-attribute:

.. code-block:: python

   {
       "emails": Many(
           Account.emails,
           {
               "value": EmailAddress.address,
               "type": EmailAddress.kind,
               "primary": EmailAddress.preferred,
           },
       )
   }

Declare the relationship with ``cascade="all, delete-orphan"``: writing the resource replaces
its entries, and the :ref:`delete-orphan <sqlalchemy:cascade_delete_orphan>` cascade deletes the
former ones. A relationship without this cascade raises a
:class:`ValueError`. The ``order_by`` of the relationship gives the order of the entries. A sort
on the attribute uses the entry marked ``primary``, or else the first entry.

Link to other resources
-----------------------

Map a complex attribute whose value is another resource with
:class:`~scim2_sqlalchemy.Link`. Give it the relationship, and the name of the resource
type of the linked resources:

- for a multi-valued attribute, such as the ``members`` of a group, give a relationship holding
  several records;
- for a single-valued attribute, such as the ``manager`` of an enterprise user or the ``owner``
  of a project, give a relationship holding one record, such as a many-to-one relationship.

.. code-block:: python

   {
       "owner": Link(ProjectRecord.owner, "User"),
       "members": Link(ProjectRecord.members, "User"),
   }

A relationship that does not hold as many records as the attribute raises a :class:`ValueError`.
The storage reads ``value`` from the identifier of the linked resource, and the display name,
``display`` or ``displayName``, from its ``displayName``, or else its ``userName``. With a provider, it also gives each entry a
``$ref``. Writing the resource changes the links: a link to an unknown resource, or an entry
without ``value``, raises a 400 error. The linked resources are never created or deleted.

A storage serves the resource type of every :class:`~scim2_sqlalchemy.Link`. It raises a
:class:`ValueError` otherwise.

Map a custom resource type
--------------------------

Map a resource type of the application the same way as a standard one. Declare its SCIM model
first: for the steps, read :doc:`scim2_models:how-to/define-custom-models`. The intranet serves
its projects, whose owner and members are users:

.. literalinclude:: ../_examples/intranet/projects.py
   :language: python
   :start-at: class UserReference
   :end-before: PROJECTS =

Map it to the project model:

.. literalinclude:: ../_examples/intranet/projects.py
   :language: python
   :start-at: PROJECTS =
   :end-before: def create_provider

Serve it next to the users. The mappings of the storage are keyed by the name of each resource
type:

.. literalinclude:: ../_examples/intranet/projects.py
   :language: python
   :pyobject: create_storage

Create a project owned by a user:

.. doctest::

   >>> from scim2_server.applications.wsgi import WSGIApplication
   >>> from sqlalchemy import create_engine
   >>> from sqlalchemy.orm import Session
   >>> from werkzeug.test import Client
   >>> from intranet.models import Base
   >>> from intranet.projects import create_provider
   >>> from intranet.projects import create_storage

   >>> engine = create_engine("sqlite://")
   >>> Base.metadata.create_all(engine)
   >>> session = Session(engine)
   >>> provider = create_provider()
   >>> client = Client(WSGIApplication(create_storage(session, provider), provider))
   >>> user = client.post("/v2/Users", json={"userName": "bjensen"}).json
   >>> project = client.post(
   ...     "/v2/Projects", json={"code": "APOLLO", "owner": {"value": user["id"]}}
   ... ).json
   >>> project["owner"]
   {'value': '...', '$ref': 'http://localhost/v2/Users/...', 'display': 'bjensen'}
   >>> session.close()
   >>> engine.dispose()

For every parameter of the mapping and of the storage, read :doc:`../reference/mapping` and
:doc:`../reference/storages`.
