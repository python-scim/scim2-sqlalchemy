"""Records without dates, whose resources get the hash of their values as version, and an Entry for the work email."""

from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import User
from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import Table
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from scim2_sqlalchemy import Entry
from scim2_sqlalchemy import Link
from scim2_sqlalchemy import ResourceMapping

from .models import ENTERPRISE
from .models import Base

plain_membership = Table(
    "plain_membership",
    Base.metadata,
    Column(
        "group_id", ForeignKey("plain_groups.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "user_id", ForeignKey("plain_users.id", ondelete="CASCADE"), primary_key=True
    ),
)


class PlainUserRecord(Base):
    __tablename__ = "plain_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_name: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str | None]
    title: Mapped[str | None]
    nick_name: Mapped[str | None]
    given_name: Mapped[str | None]
    family_name: Mapped[str | None]
    active: Mapped[bool | None]
    password: Mapped[str | None]
    email: Mapped[str | None]
    employee_number: Mapped[str | None]

    groups: Mapped[list["PlainGroupRecord"]] = relationship(
        secondary=plain_membership, back_populates="members"
    )


class PlainGroupRecord(Base):
    __tablename__ = "plain_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None]
    version: Mapped[int] = mapped_column()

    members: Mapped[list[PlainUserRecord]] = relationship(
        secondary=plain_membership, back_populates="groups"
    )

    __mapper_args__ = {"version_id_col": version}


class GeneratedVersionRecord(Base):
    """A record whose version the database generates."""

    __tablename__ = "generated_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None]
    version: Mapped[int] = mapped_column(server_default="1")

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}


def plain_users_mapping():
    return ResourceMapping(
        User[EnterpriseUser],
        PlainUserRecord,
        {
            "id": PlainUserRecord.id,
            "userName": PlainUserRecord.user_name,
            "displayName": PlainUserRecord.display_name,
            "title": PlainUserRecord.title,
            "nickName": PlainUserRecord.nick_name,
            "name.givenName": PlainUserRecord.given_name,
            "name.familyName": PlainUserRecord.family_name,
            "active": PlainUserRecord.active,
            "password": PlainUserRecord.password,
            "emails": Entry(
                {"value": PlainUserRecord.email}, type="work", primary=True
            ),
            "groups": Link(PlainUserRecord.groups, "Group", type="direct"),
            f"{ENTERPRISE}:employeeNumber": PlainUserRecord.employee_number,
        },
    )


def plain_groups_mapping(version=None):
    return ResourceMapping(
        Group,
        PlainGroupRecord,
        {
            "id": PlainGroupRecord.id,
            "displayName": PlainGroupRecord.display_name,
            "members": Link(PlainGroupRecord.members, "User", type="User"),
        },
        version=version,
    )


PLAIN_MAPPINGS = {"User": plain_users_mapping(), "Group": plain_groups_mapping()}
