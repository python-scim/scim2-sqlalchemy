Compare strings with a custom comparison key
============================================

This guide makes the storage compare strings with a comparison key other than the default one, such
as the PRECIS rules that :rfc:`RFC 7644 §5 <7644#section-5>` requires for ``userName``. A comparison
key is a function that returns the form a string is compared in: two strings are equal when their
forms are equal. The guide is for developers who serve a mapping with scim2-server, as in the
:doc:`../overview`. It does not cover the rules a comparison key must follow: for them, read
:doc:`scim2_models:how-to/compare-values`.

Give the policy to the storage
------------------------------

The storage compares strings with the :attr:`~scim2_models.ScimPolicy.comparison_key` of the policy
of its provider. Build the provider with the policy, and pass the same provider to the storage and
to the application. The intranet of the :doc:`../overview` builds its provider with
``create_provider``, which takes the policy:

.. doctest::

   >>> from unicodedata import normalize
   >>> from scim2_models import ScimPolicy
   >>> from scim2_models import default_comparison_key
   >>> from intranet.scim import create_provider

   >>> def compatibility_key(binding, value):
   ...     return default_comparison_key(binding, normalize("NFKC", value))

   >>> provider = create_provider(ScimPolicy(comparison_key=compatibility_key))

Without a provider, the storage compares strings with the default comparison key.

The comparison key of this example applies the compatibility normalization of Unicode, ``NFKC``,
before the default comparison key. It replaces the characters that only differ by their presentation
with their plain form:

- the fullwidth letters of East Asian keyboards: ``ＢＪＥＮＳＥＮ`` becomes ``BJENSEN``;
- the ligatures: ``ﬁ`` becomes ``fi``;
- the superscripts and the subscripts: ``x²`` becomes ``x2``.

The stored values keep their form: only the comparisons change. More values then compare equal. A
filter on ``ＢＪＥＮＳＥＮ`` finds the user ``bjensen``, and a second user named ``ｂｊｅｎｓｅｎ`` is refused:

.. doctest::
   :hide:

   >>> from scim2_server.applications.wsgi import WSGIApplication
   >>> from sqlalchemy import create_engine
   >>> from sqlalchemy.orm import Session
   >>> from werkzeug.test import Client
   >>> from intranet.models import Base
   >>> from intranet.scim import create_storage
   >>> engine = create_engine("sqlite://")
   >>> Base.metadata.create_all(engine)
   >>> session = Session(engine)
   >>> client = Client(WSGIApplication(create_storage(session, provider), provider))

.. doctest::

   >>> client.post("/v2/Users", json={"userName": "bjensen"}).status_code
   201
   >>> response = client.get("/v2/Users", query_string={"filter": 'userName eq "ＢＪＥＮＳＥＮ"'})
   >>> response.json["totalResults"]
   1
   >>> client.post("/v2/Users", json={"userName": "ｂｊｅｎｓｅｎ"}).status_code
   409

.. doctest::
   :hide:

   >>> session.close()
   >>> engine.dispose()

The comparison key of this example applies to every string attribute, such as ``displayName``, where
``x²`` and ``x2`` become equal too. To limit it to some attributes, test the URN of the binding,
``binding.urn``, and return ``default_comparison_key(binding, value)`` for the others.

Check what the database supports
--------------------------------

The comparisons happen in two places:

- :doc:`scim2-models <scim2_models:index>` and :doc:`scim2-server <scim2_server:index>` compare in
  Python, with the comparison key itself. They do so for the path filters of PATCH requests, such as
  ``emails[value eq "…"]``, and to find the values a PATCH request adds or removes.
- The storage compares in SQL, for the filters and the sorts of searches, and for the uniqueness
  checks.

Both give the same results only when the database follows the comparison key:

- On SQLite, the storage calls the comparison key for each row, and follows it exactly. No index
  serves these filters.
- On PostgreSQL, the storage cannot call the comparison key. It compares the strings in the form of
  the default comparison key, and warns when the policy sets another one. With the comparison key of
  this guide, a search on ``ＢＪＥＮＳＥＮ`` finds nothing, and ``ｂｊｅｎｓｅｎ`` can be created next to
  ``bjensen``. A PATCH request still compares in Python, and takes the two names as equal.

On PostgreSQL, keep the default comparison key. Even then, a few letters differ between Python and
the database, such as ``İ`` and the final sigma. For the details of each database, read
:doc:`../explanation`.
