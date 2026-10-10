import datetime
import uuid

from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import User
from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import Table
from sqlalchemy import func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Link
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping

ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


class Base(DeclarativeBase):
    pass


membership = Table(
    "membership",
    Base.metadata,
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str | None]
    user_name: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str | None]
    title: Mapped[str | None]
    nick_name: Mapped[str | None]
    user_type: Mapped[str | None] = mapped_column(default="Employee")
    given_name: Mapped[str | None]
    family_name: Mapped[str | None]
    status: Mapped[str | None]
    password_hash: Mapped[str | None]
    employee_number: Mapped[str | None]
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()

    emails: Mapped[list["EmailRecord"]] = relationship(
        cascade="all, delete-orphan", order_by="EmailRecord.id"
    )
    groups: Mapped[list["GroupRecord"]] = relationship(
        secondary=membership, back_populates="members"
    )
    manager: Mapped["UserRecord | None"] = relationship(remote_side="UserRecord.id")

    __mapper_args__ = {"version_id_col": version}

    @hybrid_property
    def active(self) -> bool | None:
        return None if self.status is None else self.status == "enabled"

    @active.inplace.setter
    def _active_setter(self, value: bool | None) -> None:
        self.status = None if value is None else ("enabled" if value else "disabled")

    @active.inplace.expression
    @classmethod
    def _active_expression(cls):
        return cls.status == "enabled"

    @hybrid_property
    def password(self) -> str | None:
        return self.password_hash

    @password.inplace.setter
    def _password_setter(self, value: str | None) -> None:
        self.password_hash = None if value is None else f"hashed:{value}"

    @hybrid_property
    def lower_name(self) -> str:
        return self.user_name.lower()

    @lower_name.expression
    def lower_name(cls):
        return func.lower(cls.user_name)


class EmailRecord(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    value: Mapped[str | None]
    type: Mapped[str | None]
    primary: Mapped[bool | None]


class GroupRecord(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None]
    display_name: Mapped[str | None]
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()

    members: Mapped[list[UserRecord]] = relationship(
        secondary=membership, back_populates="groups"
    )

    __mapper_args__ = {"version_id_col": version}


class BadgeRecord(Base):
    """A record outside SCIM, whose user cannot be deleted while it exists."""

    __tablename__ = "badges"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


def users_mapping(changes=None):
    """Return a new mapping of the users, with some paths changed, or removed when None."""
    attributes = {
        "id": UserRecord.id,
        "externalId": UserRecord.external_id,
        "userName": UserRecord.user_name,
        "displayName": UserRecord.display_name,
        "title": UserRecord.title,
        "userType": UserRecord.user_type,
        "nickName": UserRecord.nick_name,
        "locale": UserRecord.lower_name,
        "name.givenName": UserRecord.given_name,
        "name.familyName": UserRecord.family_name,
        "active": UserRecord.active,
        "password": Attribute(UserRecord.password, readable=False),
        "emails": Many(
            UserRecord.emails,
            {
                "value": EmailRecord.value,
                "type": EmailRecord.type,
                "primary": EmailRecord.primary,
            },
        ),
        "groups": Link(UserRecord.groups, "Group"),
        f"{ENTERPRISE}:employeeNumber": UserRecord.employee_number,
        f"{ENTERPRISE}:manager": Link(UserRecord.manager, "User"),
        "meta.created": UserRecord.created,
        "meta.lastModified": UserRecord.last_modified,
    }
    attributes |= changes or {}
    attributes = {
        path: value for path, value in attributes.items() if value is not None
    }
    return ResourceMapping(
        User[EnterpriseUser], UserRecord, attributes, version=UserRecord.version
    )


def groups_mapping(changes=None):
    """Return a new mapping of the groups, with some paths changed, or removed when None."""
    attributes = {
        "id": GroupRecord.id,
        "externalId": GroupRecord.external_id,
        "displayName": GroupRecord.display_name,
        "members": Link(GroupRecord.members, "User"),
        "meta.created": GroupRecord.created,
        "meta.lastModified": GroupRecord.last_modified,
    }
    attributes |= changes or {}
    attributes = {
        path: value for path, value in attributes.items() if value is not None
    }
    return ResourceMapping(Group, GroupRecord, attributes, version=GroupRecord.version)


USERS = users_mapping()
GROUPS = groups_mapping()

MAPPINGS = {"User": USERS, "Group": GROUPS}
