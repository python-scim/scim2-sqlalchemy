# scim2-sqlalchemy

Store [SCIM](https://scim.cloud) resources in the tables of an application, with the [SQLAlchemy](https://www.sqlalchemy.org) ORM.

A mapping declares which column holds each SCIM attribute.
A storage then reads, writes, filters, sorts and pages the resources for [scim2-server](https://scim2-server.readthedocs.io), which serves the SCIM protocol of [RFC 7643](https://www.rfc-editor.org/rfc/rfc7643) and [RFC 7644](https://www.rfc-editor.org/rfc/rfc7644).

```python
from scim2_models import User
from scim2_sqlalchemy import Many, ResourceMapping, SqlAlchemyStorage

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
```

## Installation

```console
$ pip install scim2-sqlalchemy
```

The asynchronous storage needs the `asyncio` extra:

```console
$ pip install "scim2-sqlalchemy[asyncio]"
```

## Documentation

The [documentation](https://scim2-sqlalchemy.readthedocs.io) builds a SCIM server over existing tables, and describes the mappings, the transactions and the public API.

- Source code: [github.com/python-scim/scim2-sqlalchemy](https://github.com/python-scim/scim2-sqlalchemy)
- Bugtracker: [github.com/python-scim/scim2-sqlalchemy/issues](https://github.com/python-scim/scim2-sqlalchemy/issues)

## Contributing

Read the [contributing guide](https://scim2-sqlalchemy.readthedocs.io/en/latest/contributing.html).

## License

scim2-sqlalchemy is published under the terms of the [Apache 2.0 license](LICENSE).
