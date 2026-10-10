How the storage works
=====================

This page explains why the SQLAlchemy storage behaves as it does: why it maps existing models,
how its queries keep the meaning of SCIM, and why it refuses some writes. It is for developers
who already use the storage and want to understand its choices, away from the keyboard. The
steps to use it are in the :doc:`overview` and the how-to guides. The rules every storage
follows are in :doc:`scim2_server:explanation/storage`.

The models of the application stay the source of truth
------------------------------------------------------

An application that adds SCIM already has its data, in tables designed for its own needs. The
storage maps these tables as they are, rather than bringing tables of its own. A table designed
for SCIM would hold the resources as JSON documents, which databases filter and index unevenly,
and would leave the application with two copies of its users.

A single declaration, the :class:`~scim2_sqlalchemy.ResourceMapping`, serves every task of the
storage: reading a record, writing it, filtering and sorting. A storage written by hand
describes the tables twice, once for the conversions and once for the queries, and the two
descriptions drift apart. For an example of such a storage, read
:doc:`scim2_server:how-to/serve-an-existing-data-model`.

The mapping checks its paths against the SCIM model when it is created. A mistake shows up when
the application starts, rather than on the first request that needs the attribute.

Filters keep the meaning of SCIM
--------------------------------

The storage turns a SCIM filter into a SQL condition, so that the database returns the matching
rows only, and counts them for ``totalResults``. Filtering in Python after the query would make
the total and the pages wrong. The condition gives the result that
:meth:`ScimFilter.match <scim2_models.ScimFilter.match>` gives on the same resources, which
differs from a direct translation in several places:

- SQL compares nothing to ``NULL``: ``title != 'Boss'`` is unknown when ``title`` is ``NULL``. In
  SCIM, ``title ne "Boss"`` holds for a user without a title. The storage writes every condition
  so that it is either true or false, never unknown, and a negation then keeps its meaning.
- A filter on a multi-valued attribute holds when one of its values matches: ``emails co "work"``
  becomes an ``EXISTS`` on the email table. ``emails ne "x"`` holds when no email is ``x``,
  while ``emails[value ne "x"]`` holds when one email is not ``x``.
- A value selection, such as ``emails[type eq "work" and primary eq true]``, applies all its
  conditions to the same entry.
- A string compares in the form the comparison key of the policy gives it. By default, the
  comparison key normalizes the string to NFC, and lowers it unless the schema declares it
  ``caseExact``. Each database reproduces the comparison key its own way, as the next section
  describes.

An attribute the resource type does not declare matches no resource
(:rfc:`RFC 7644 §3.4.2.1 <7644#section-3.4.2.1>`). An attribute it declares but the mapping does
not store raises an ``invalidFilter`` error instead: the storage cannot evaluate it, and an empty
result would be a wrong answer.

Strings compare as the policy says
----------------------------------

The :attr:`comparison_key <scim2_models.ScimPolicy.comparison_key>` of a
:class:`~scim2_models.ScimPolicy` gives the form strings are compared in. scim2-models applies it in
Python, to filters, sorts and PATCH operations. For the reasons behind the comparison key, read
:doc:`scim2_models:explanation/comparisons`. The storage follows the policy of the provider it
receives, or the default policy without a provider. It reproduces the comparison key in SQL, as far
as the database allows:

- SQLite runs Python functions. The storage gives each connection a SQL function that calls the key,
  so filters, sorts and uniqueness checks compare exactly as in Python, whatever the comparison key.
  The function runs once for each row, and no index serves it. The ``co``, ``sw`` and ``ew`` filters
  use ``instr()`` and ``substr()``, since the ``LIKE`` of SQLite ignores the case of ASCII letters,
  even for a ``caseExact`` attribute.
- PostgreSQL normalizes both sides to NFC with ``normalize()``, and lowers them with ``lower()``.
  This approaches the default comparison key without reaching it. ``lower()`` follows the locale of
  the database, and only lowers ASCII letters under the ``C`` locale. Even under a UTF-8 locale, a
  few letters differ, such as ``İ`` and the final sigma. ``normalize()`` needs PostgreSQL 13 and a
  database encoded in UTF-8. The comparison uses the ``C`` collation, which orders strings by code
  point, as scim2-models does, whatever the collation of the column. An index that serves these
  filters is an index on the same expression, with the same collation. With a comparison key other
  than the default one, the storage warns that it only approaches it.
- Other databases lower both sides with their own ``lower()``. MySQL and MariaDB are not
  supported: their default collations ignore the case and the accents, even for a ``caseExact``
  attribute.

Where ``LIKE`` serves the ``co``, ``sw`` and ``ew`` filters, the ``%`` and ``_`` characters of the
value are escaped, so that they match themselves.

A string the comparison key refuses is equal to no other. A filter comparing an attribute with such
a string holds only for ``ne``. On SQLite, a stored string the comparison key refuses also matches
``ne`` only, and sorts with the missing values.

Sorting follows RFC 7644
------------------------

:rfc:`RFC 7644 §3.4.2.3 <7644#section-3.4.2.3>` places the resources without a value last in an
ascending sort, and first in a descending one. Databases disagree on where ``NULL`` goes, so the
storage states the place of ``NULL`` in every query. A multi-valued attribute sorts on its
``primary`` entry, or else on its first one.

The storage always ends the order with the identifier of the record. Two resources sharing a
sort value would otherwise come back in any order, and a resource could appear on two pages, or
on none.

A search at the root of the server sorts and pages the resources of every type as one
collection. The storage unites the tables in one SQL query, which returns the resource type and
the identifier of each resource of the page. It then loads the records of each type. A resource
type without the sort attribute sorts its resources as having no value. Between two resources of
different types that share a sort value, the order of the resource types decides.

Cursors give stable pages
-------------------------

The storage pages with ``startIndex``, and with the cursors of :rfc:`9865`. With a cursor, the
position of a page holds the sort value and the identifier of a resource: the last one of the
page for the next page, and the first one for the previous page. The next page selects the
resources after them in the sort order. A resource created or deleted between two pages does not
move the other ones. :doc:`scim2_server:explanation/pagination` explains what these stable pages
guarantee, and :doc:`scim2_server:how-to/page-with-cursors` explains how to announce the
cursors.

The sort value of a position is the value the database compares, after the comparison key of the
policy. The storage writes the condition on the sort value by hand, since a row comparison would
not place ``NULL`` where the sort does. At the root, the position also holds the resource type,
which orders the resources of different types that share a sort value.

The storage reads one resource more than ``count``, to know whether a page follows. It only knows
it in the direction it reads: forward for a next page, backward for a previous page. In the other
direction, it assumes that the page the client comes from still exists. When all the resources of
that page were deleted meanwhile, the client gets an empty page without cursors, and starts again
from the first page.

Each page shows the resources as they are when it is read. The storage does not give a snapshot
of the collection as it was on the first page.

Writes that cannot be stored are refused
----------------------------------------

A mapping rarely stores every attribute of a schema. A server that accepted a value and dropped
it would answer as if it kept the value, and the client would only notice when it reads the
resource again. The storage refuses such a write with a 400 error instead:

- a value for an attribute no column stores gets ``invalidValue``;
- a different value for an attribute the mapping only reads gets ``mutability``;
- the removal of a value whose column cannot be ``NULL`` gets ``mutability``.

The storage compares each value with the stored resource. A client that sends back a resource it
has read sends the computed values and the read-only values unchanged, and the write succeeds.
The attributes the schema declares ``readOnly`` are left out of the comparison, since
:rfc:`RFC 7644 §3.5.1 <7644#section-3.5.1>` has the server ignore them.

These refusals catch the attributes the published schemas announce and the mapping does not
store. Publishing the schemas of the mappings, as :doc:`how-to/publish-the-stored-attributes`
does, keeps clients from sending them at all. The refusals then only protect against a mapping
that drifts away from the models written by hand.

The server builds the URL of a link
-----------------------------------

The ``$ref`` of a link, such as the ``$ref`` of a group member, is the URL of the linked
resource. The storage knows the linked record and its resource type, but not the URL the server
answers at, which depends on the proxy, the host and the tenant. It returns a ``$ref`` relative to
the SCIM root, such as ``Users/2819c223``, which :rfc:`RFC 7643 §2.3.7 <7643#section-2.3.7>`
allows. scim2-server turns it into an absolute URL, as it does for ``meta.location``.

The storage stores the ``value`` of a link only. It ignores the ``$ref`` a client sends, and
refuses an entry without ``value``: storing nothing for it would lose the link.

Versions protect concurrent writes
----------------------------------

The version of a resource comes from the :ref:`version counter <sqlalchemy:mapper_version_counter>`
of its SQLAlchemy model. SQLAlchemy increments the column on every write, and adds the version it
read to the ``WHERE`` clause of the ``UPDATE``. The storage first compares the stored version with
the one the client sent in ``If-Match``, and answers 412 when they differ. A write by another
transaction between the read and the write matches no row: SQLAlchemy raises
:exc:`~sqlalchemy.orm.exc.StaleDataError`, which the storage turns into a 412 too.

Unique values are checked twice
-------------------------------

Before a write, the storage searches for another record holding a unique value of the resource,
such as its ``userName``, and answers 409 when it finds one. The search follows the case rules of
the attribute, so ``BJensen`` clashes with ``bjensen``, which a plain unique constraint does not
catch. A unique constraint of the database remains the guard against two concurrent writes. When
a flush fails on a constraint, the storage searches again, and answers 409 when the value is now
taken. Any other :exc:`~sqlalchemy.exc.IntegrityError` goes through unchanged.

The application owns the transaction
------------------------------------

The storage :ref:`flushes <sqlalchemy:session_flushing>` each write, so that the database fills the
identifiers and checks its constraints, and never commits. The application, or its web framework,
already decides when a request ends and whether its changes are kept. Each operation runs in a
:ref:`savepoint <sqlalchemy:session_begin_nested>`, so that a failed operation leaves the session
usable, and a bulk request goes on after a failure.

The asynchronous storage loads every collection of a record with the record. An
:class:`~sqlalchemy.ext.asyncio.AsyncSession` cannot load a relationship :ref:`lazily
<sqlalchemy:asyncio_orm_avoid_lazyloads>`, on first access. Both storages load the same way, so they
send the same queries.

Limits
------

A search or a read only loads the collections its response keeps, following the ``attributes``
and ``excludedAttributes`` parameters. With ``excludedAttributes=members``, the members of the
groups are not loaded. The storage still loads every column of the records, which costs little
next to a collection. A write reads the whole resource. scim2-server then removes the attributes
the response does not keep.
