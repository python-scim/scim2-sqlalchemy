"""Serve the accounts and the teams of the intranet as SCIM users and groups."""

from scim2_models import Group
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_models import User
from scim2_server.utils import load_default_service_provider_config

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Link
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping
from scim2_sqlalchemy import SqlAlchemyStorage

from .models import Account
from .models import EmailAddress
from .models import Team

USERS = ResourceMapping(
    User,
    Account,
    {
        "id": Account.id,
        "externalId": Account.external_id,
        "userName": Account.login,
        "name.givenName": Account.first_name,
        "name.familyName": Account.last_name,
        "displayName": Account.full_name,
        "active": Account.active,
        "password": Attribute(Account.password, readable=False),
        "emails": Many(
            Account.emails,
            {
                "value": EmailAddress.address,
                "type": EmailAddress.kind,
                "primary": EmailAddress.preferred,
            },
        ),
        "groups": Link(Account.teams, "Group"),
        "meta.created": Account.created_at,
        "meta.lastModified": Account.updated_at,
    },
    version=Account.revision,
)

GROUPS = ResourceMapping(
    Group,
    Team,
    {
        "id": Team.id,
        "externalId": Team.external_id,
        "displayName": Team.name,
        "members": Link(Team.members, "User"),
        "meta.created": Team.created_at,
        "meta.lastModified": Team.updated_at,
    },
    version=Team.revision,
)


def create_provider():
    return ScimProvider.from_discovery(
        [*USERS.schemas(), *GROUPS.schemas()],
        [
            ResourceType.from_resource(USERS.model),
            ResourceType.from_resource(GROUPS.model),
        ],
        config=load_default_service_provider_config(),
    )


def create_storage(session, provider):
    return SqlAlchemyStorage(
        {"User": USERS, "Group": GROUPS}, lambda: session, provider=provider
    )
