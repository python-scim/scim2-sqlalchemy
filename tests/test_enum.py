import datetime
import enum
import uuid

import pytest
from scim2_models import InvalidValueException
from scim2_models import SearchRequest
from scim2_models import User
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping


class Color(enum.Enum):
    Red = "Rouge"
    blue = "bleu"
    Green = "Vert"


class Base(DeclarativeBase):
    pass


class ColoredUser(Base):
    """A user whose title column stores the names of the members, and whose external identifier stores their values."""

    __tablename__ = "colored_users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_name: Mapped[str] = mapped_column(unique=True)
    title: Mapped[Color | None] = mapped_column(Enum(Color, name="title_color"))
    external_id: Mapped[Color | None] = mapped_column(
        Enum(
            Color,
            name="external_color",
            values_callable=lambda members: [member.value for member in members],
        )
    )
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()

    emails: Mapped[list["ColoredEmail"]] = relationship(
        cascade="all, delete-orphan", order_by="ColoredEmail.id"
    )

    __mapper_args__ = {"version_id_col": version}


class ColoredEmail(Base):
    __tablename__ = "colored_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("colored_users.id"))
    value: Mapped[str]
    type: Mapped[Color | None] = mapped_column(Enum(Color, name="email_color"))


COLORED_USERS = ResourceMapping(
    User,
    ColoredUser,
    {
        "id": ColoredUser.id,
        "userName": ColoredUser.user_name,
        "title": ColoredUser.title,
        "externalId": ColoredUser.external_id,
        "emails": Many(
            ColoredUser.emails,
            {"value": ColoredEmail.value, "type": ColoredEmail.type},
        ),
        "meta.created": ColoredUser.created,
        "meta.lastModified": ColoredUser.last_modified,
    },
    version=ColoredUser.version,
)


@pytest.fixture
def storage(database_url, storage_factory, user_type):
    """Return a storage of users with enumerated columns: a with Red, b with blue, c with Green, d without."""
    engine = create_engine(database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()
    storage = storage_factory(mappings={"User": COLORED_USERS})
    for user_name, title, external_id in [
        ("a", "Red", "Rouge"),
        ("b", "blue", "bleu"),
        ("c", "Green", "Vert"),
        ("d", None, None),
    ]:
        emails = [{"value": f"{user_name}@example.org", "type": title}]
        storage.create(
            user_type,
            User(
                user_name=user_name,
                title=title,
                external_id=external_id,
                emails=emails,
            ),
        )
    return storage


def user_names(storage, user_type, **parameters):
    _, resources = storage.search([user_type], SearchRequest(**parameters))
    return [resource.user_name for resource in resources]


def test_an_enumerated_column_reads_back_the_string_it_stores(storage, user_type):
    """A resource holds the string the column stores: the name of the member, or its value with values_callable."""
    _, (user,) = storage.search([user_type], SearchRequest(filter='userName eq "a"'))

    assert user.title == "Red"
    assert user.external_id == "Rouge"
    assert user.emails[0].type == "Red"


@pytest.mark.parametrize(
    ("scim_filter", "expected"),
    [
        ('title eq "blue"', ["b"]),
        ('title eq "BLUE"', ["b"]),
        ('title eq "bleu"', []),
        ('title gt "h"', ["a"]),
        ('title co "e"', ["a", "b", "c"]),
        ("title pr", ["a", "b", "c"]),
        ('externalId eq "bleu"', ["b"]),
        ('externalId eq "Bleu"', []),
        ('externalId gt "S"', ["b", "c"]),
        ('emails[type eq "green"]', ["c"]),
        ("emails.type pr", ["a", "b", "c"]),
    ],
)
def test_an_enumerated_column_is_filtered_on_the_string_it_stores(
    storage, user_type, scim_filter, expected
):
    """A filter compares the string the resource holds, as for any string column."""
    assert sorted(user_names(storage, user_type, filter=scim_filter)) == expected


@pytest.mark.parametrize(
    ("sort_by", "sort_order", "expected"),
    [
        ("title", "ascending", ["b", "c", "a", "d"]),
        ("title", "descending", ["d", "a", "c", "b"]),
        ("externalId", "ascending", ["a", "c", "b", "d"]),
    ],
)
def test_an_enumerated_column_sorts_on_the_string_it_stores(
    storage, user_type, sort_by, sort_order, expected
):
    """The order is the one of the strings, and not the order the members are declared in."""
    assert (
        user_names(storage, user_type, sort_by=sort_by, sort_order=sort_order)
        == expected
    )


@pytest.mark.parametrize(
    "user",
    [
        User(user_name="e", title="Purple"),
        User(user_name="e", title="Rouge"),
        User(user_name="e", external_id="Red"),
        User(user_name="e", emails=[{"value": "e@example.org", "type": "Purple"}]),
    ],
)
def test_an_enumerated_column_refuses_a_string_it_does_not_store(
    storage, user_type, user
):
    """A string outside the enumeration is refused with a 400, rather than failing in the database."""
    with pytest.raises(InvalidValueException):
        storage.create(user_type, user)
