"""Mailbox records for the storage contract.

Their unique values are sub-attributes and multi-valued attributes.
"""

import datetime
import uuid

from scim2_server.testing import Mailbox
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from scim2_sqlalchemy import Entry
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping

from .models import MAPPINGS
from .models import Base


class MailboxRecord(Base):
    """A mailbox whose addresses and aliases are in child tables, with a column for its work address."""

    __tablename__ = "mailboxes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str | None]
    owner: Mapped[str | None]
    owner_display: Mapped[str | None]
    work_address: Mapped[str | None]
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()

    addresses: Mapped[list["AddressRecord"]] = relationship(
        cascade="all, delete-orphan", order_by="AddressRecord.id"
    )
    aliases: Mapped[list["AliasRecord"]] = relationship(
        cascade="all, delete-orphan", order_by="AliasRecord.id"
    )

    __mapper_args__ = {"version_id_col": version}


class AddressRecord(Base):
    __tablename__ = "mailbox_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE")
    )
    value: Mapped[str | None]
    type: Mapped[str | None]


class AliasRecord(Base):
    __tablename__ = "mailbox_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE")
    )
    value: Mapped[str]


class MailboxView(Base):
    """Another class of the mailbox table, without inheritance."""

    __table__ = MailboxRecord.__table__
    __mapper_args__ = {"version_id_col": __table__.c.version}


class SharedMailboxRecord(Base):
    """A mailbox holding a single address, in a column of its record."""

    __tablename__ = "shared_mailboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None]
    address: Mapped[str | None]
    address_type: Mapped[str | None]
    owner: Mapped[str | None]
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()

    aliases: Mapped[list["SharedAliasRecord"]] = relationship(
        cascade="all, delete-orphan", order_by="SharedAliasRecord.id"
    )

    __mapper_args__ = {"version_id_col": version}


class SharedAliasRecord(Base):
    __tablename__ = "shared_mailbox_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("shared_mailboxes.id", ondelete="CASCADE")
    )
    value: Mapped[str]


class GuardedMailboxRecord(Base):
    """A mailbox whose application refuses an address held twice, with a unique index."""

    __tablename__ = "guarded_mailboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None]
    version: Mapped[int] = mapped_column()

    addresses: Mapped[list["GuardedAddressRecord"]] = relationship(
        cascade="all, delete-orphan", order_by="GuardedAddressRecord.id"
    )

    __mapper_args__ = {"version_id_col": version}


class GuardedAddressRecord(Base):
    __tablename__ = "guarded_mailbox_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("guarded_mailboxes.id", ondelete="CASCADE")
    )
    value: Mapped[str | None] = mapped_column(unique=True)
    type: Mapped[str | None]


def mailboxes_mapping(changes=None):
    """Return a mapping of the mailboxes, with some paths changed."""
    attributes = {
        "id": MailboxRecord.id,
        "displayName": MailboxRecord.display_name,
        "addresses": Many(
            MailboxRecord.addresses,
            {"value": AddressRecord.value, "type": AddressRecord.type},
        ),
        "aliases": Many(MailboxRecord.aliases, AliasRecord.value),
        "owner.value": MailboxRecord.owner,
        "owner.display": MailboxRecord.owner_display,
        "meta.created": MailboxRecord.created,
        "meta.lastModified": MailboxRecord.last_modified,
    }
    attributes |= changes or {}
    return ResourceMapping(
        Mailbox, MailboxRecord, attributes, version=MailboxRecord.version
    )


def work_addresses_mapping(record):
    """Return a mapping of the mailboxes of a class, reading their work address only."""
    return ResourceMapping(
        Mailbox,
        record,
        {
            "id": record.id,
            "displayName": record.display_name,
            "addresses": Entry({"value": record.work_address}, type="work"),
            "meta.created": record.created,
            "meta.lastModified": record.last_modified,
        },
        version=record.version,
    )


def shared_mailboxes_mapping():
    return ResourceMapping(
        Mailbox,
        SharedMailboxRecord,
        {
            "id": SharedMailboxRecord.id,
            "displayName": SharedMailboxRecord.display_name,
            "addresses": Entry(
                {
                    "value": SharedMailboxRecord.address,
                    "type": SharedMailboxRecord.address_type,
                }
            ),
            "aliases": Many(SharedMailboxRecord.aliases, SharedAliasRecord.value),
            "owner.value": SharedMailboxRecord.owner,
            "meta.created": SharedMailboxRecord.created,
            "meta.lastModified": SharedMailboxRecord.last_modified,
        },
        version=SharedMailboxRecord.version,
    )


def guarded_mailboxes_mapping():
    return ResourceMapping(
        Mailbox,
        GuardedMailboxRecord,
        {
            "id": GuardedMailboxRecord.id,
            "displayName": GuardedMailboxRecord.display_name,
            "addresses": Many(
                GuardedMailboxRecord.addresses,
                {
                    "value": GuardedAddressRecord.value,
                    "type": GuardedAddressRecord.type,
                },
            ),
        },
        version=GuardedMailboxRecord.version,
    )


MAILBOX_MAPPINGS = MAPPINGS | {
    "Mailbox": mailboxes_mapping(),
    "SharedMailbox": shared_mailboxes_mapping(),
}
"""The mappings of the storage contract, with the mailboxes of the tests of uniqueness."""
