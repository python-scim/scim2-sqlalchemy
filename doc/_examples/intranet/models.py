"""The tables of an intranet application, before it serves SCIM."""

import datetime
import hashlib
import os

from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import Table
from sqlalchemy import func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


class Base(DeclarativeBase):
    pass


team_members = Table(
    "team_members",
    Base.metadata,
    Column("team_id", ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "account_id", ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None]
    login: Mapped[str] = mapped_column(unique=True)
    first_name: Mapped[str | None]
    last_name: Mapped[str | None]
    active: Mapped[bool | None] = mapped_column(default=True)
    office: Mapped[str | None]
    password_hash: Mapped[str | None]
    created_at: Mapped[datetime.datetime]
    updated_at: Mapped[datetime.datetime]
    revision: Mapped[int] = mapped_column()

    emails: Mapped[list["EmailAddress"]] = relationship(
        cascade="all, delete-orphan", order_by="EmailAddress.id"
    )
    teams: Mapped[list["Team"]] = relationship(
        secondary=team_members, back_populates="members"
    )

    __mapper_args__ = {"version_id_col": revision}

    @hybrid_property
    def full_name(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip() or None

    @full_name.expression
    def full_name(cls):
        return func.nullif(
            func.trim(
                func.coalesce(cls.first_name, "")
                + " "
                + func.coalesce(cls.last_name, "")
            ),
            "",
        )

    @hybrid_property
    def password(self):
        return self.password_hash

    @password.inplace.setter
    def _password_setter(self, value):
        self.password_hash = None if value is None else hash_password(value)


class EmailAddress(Base):
    __tablename__ = "email_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    address: Mapped[str]
    kind: Mapped[str | None]
    preferred: Mapped[bool | None]


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None]
    name: Mapped[str]
    created_at: Mapped[datetime.datetime]
    updated_at: Mapped[datetime.datetime]
    revision: Mapped[int] = mapped_column()

    members: Mapped[list[Account]] = relationship(
        secondary=team_members, back_populates="teams"
    )

    __mapper_args__ = {"version_id_col": revision}
