"""Serve the projects of the intranet, a resource type of its own, and the office of each account."""

import datetime
import uuid
from typing import Annotated

from pydantic import Field
from scim2_models import URN
from scim2_models import ComplexAttribute
from scim2_models import Extension
from scim2_models import Mutability
from scim2_models import Reference
from scim2_models import Required
from scim2_models import Resource
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_models import Uniqueness
from scim2_models import User
from scim2_server.utils import load_default_service_provider_config
from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from scim2_sqlalchemy import Link
from scim2_sqlalchemy import ResourceMapping
from scim2_sqlalchemy import SqlAlchemyStorage

from .models import Account
from .models import Base


class Workplace(Extension):
    __schema__ = URN("urn:example:params:scim:schemas:extension:intranet:2.0:User")
    office: str | None = None


ACCOUNTS = ResourceMapping(
    User[Workplace],
    Account,
    {
        "id": Account.id,
        "userName": Account.login,
        "urn:example:params:scim:schemas:extension:intranet:2.0:User:office": Account.office,
        "meta.created": Account.created_at,
        "meta.lastModified": Account.updated_at,
    },
    version=Account.revision,
)


project_members = Table(
    "project_members",
    Base.metadata,
    Column(
        "project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "account_id", ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    ),
)


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str | None]
    created_at: Mapped[datetime.datetime]
    updated_at: Mapped[datetime.datetime]
    revision: Mapped[int] = mapped_column()
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))

    owner: Mapped[Account | None] = relationship()
    members: Mapped[list[Account]] = relationship(secondary=project_members)

    __mapper_args__ = {"version_id_col": revision}


class UserReference(ComplexAttribute):
    value: Annotated[str | None, Mutability.immutable] = None
    ref: Annotated[Reference["User"] | None, Mutability.immutable] = Field(
        None, serialization_alias="$ref", validation_alias="$ref"
    )
    display: str | None = None


class Project(Resource):
    __schema__ = URN("urn:example:params:scim:schemas:intranet:2.0:Project")
    code: Annotated[str | None, Required.true, Uniqueness.server] = None
    title: str | None = None
    owner: UserReference | None = None
    members: list[UserReference] | None = None


PROJECTS = ResourceMapping(
    Project,
    ProjectRecord,
    {
        "id": ProjectRecord.id,
        "code": ProjectRecord.code,
        "title": ProjectRecord.title,
        "owner": Link(ProjectRecord.owner, "User"),
        "members": Link(ProjectRecord.members, "User"),
        "meta.created": ProjectRecord.created_at,
        "meta.lastModified": ProjectRecord.updated_at,
    },
    version=ProjectRecord.revision,
)


def create_provider():
    return ScimProvider.from_discovery(
        [*ACCOUNTS.schemas(), *PROJECTS.schemas()],
        [
            ResourceType.from_resource(ACCOUNTS.model),
            ResourceType.from_resource(PROJECTS.model),
        ],
        config=load_default_service_provider_config(),
    )


def create_storage(session, provider):
    return SqlAlchemyStorage(
        {"User": ACCOUNTS, "Project": PROJECTS}, lambda: session, provider=provider
    )
