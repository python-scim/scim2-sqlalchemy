import datetime
import uuid

import pytest
from scim2_models import URN
from scim2_models import ComplexAttribute
from scim2_models import Extension
from scim2_models import InvalidFilterException
from scim2_models import InvalidPathException
from scim2_models import Mutability
from scim2_models import MutabilityException
from scim2_models import SearchRequest
from scim2_models import User
from sqlalchemy import ForeignKey
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping

TAGGED = "urn:example:params:scim:schemas:extension:tagged:2.0:User"


class Badge(ComplexAttribute):
    lines: list[str] | None = None


class Tagged(Extension):
    __schema__ = URN(TAGGED)

    tags: list[str] | None = None
    badge: Badge | None = None


class Base(DeclarativeBase):
    pass


class TaggedUser(Base):
    __tablename__ = "tagged_users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_name: Mapped[str] = mapped_column(unique=True)
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()

    tags: Mapped[list["Tag"]] = relationship(
        cascade="all, delete-orphan", order_by="Tag.id"
    )

    __mapper_args__ = {"version_id_col": version}


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tagged_users.id"))
    value: Mapped[str]


def tagged_mapping(tags=Tag.value, **changes):
    attributes = {
        "id": TaggedUser.id,
        "userName": TaggedUser.user_name,
        f"{TAGGED}:tags": Many(TaggedUser.tags, tags),
        "meta.created": TaggedUser.created,
        "meta.lastModified": TaggedUser.last_modified,
    }
    return ResourceMapping(
        User[Tagged],
        TaggedUser,
        attributes | changes,
        version=TaggedUser.version,
    )


def tagged(user_name, tags):
    user = User[Tagged](user_name=user_name)
    user[Tagged] = Tagged(tags=tags)
    return user


@pytest.fixture
def factory(database_url, storage_factory):
    """Return a factory of storages of tagged users, over empty tables."""
    engine = create_engine(database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()
    return lambda mapping=None: storage_factory(
        mappings={"User": mapping or tagged_mapping()}
    )


@pytest.fixture
def storage(factory, user_type):
    """Return a storage of users a to e, holding tags in the order they were sent."""
    storage = factory()
    for user_name, tags in [
        ("a", ["red", "Blue", "red"]),
        ("b", ["green"]),
        ("c", [""]),
        ("d", []),
        ("e", ["blue", "amber"]),
    ]:
        storage.create(user_type, tagged(user_name, tags))
    return storage


def user_names(storage, user_type, **parameters):
    page = storage.search([user_type], SearchRequest(**parameters))
    return [resource.user_name for resource in page.resources]


def tags_of(storage, user_type, user_name):
    page = storage.search(
        [user_type], SearchRequest(filter=f'userName eq "{user_name}"')
    )
    (user,) = page.resources
    return user[Tagged] and user[Tagged].tags


def test_the_values_are_read_in_order_with_their_duplicates(storage, user_type):
    """The storage reads back the list a client sent, and an empty list as no value."""
    assert tags_of(storage, user_type, "a") == ["red", "Blue", "red"]
    assert tags_of(storage, user_type, "c") == [""]
    assert tags_of(storage, user_type, "d") is None


def test_writing_the_resource_replaces_the_values(storage, user_type):
    """The former values are deleted, and the new ones stored."""
    page = storage.search([user_type], SearchRequest(filter='userName eq "b"'))
    (user,) = page.resources
    user[Tagged].tags = ["violet", "green"]

    user = storage.update(user_type, user)

    assert user[Tagged].tags == ["violet", "green"]


@pytest.mark.parametrize(
    ("scim_filter", "expected"),
    [
        (f'{TAGGED}:tags eq "blue"', ["a", "e"]),
        (f'{TAGGED}:tags co "EE"', ["b"]),
        (f'{TAGGED}:tags ne "red"', ["b", "c", "d", "e"]),
        (f"{TAGGED}:tags pr", ["a", "b", "e"]),
        (f'not ({TAGGED}:tags eq "red")', ["b", "c", "d", "e"]),
        (f'{TAGGED}:tags[value eq "amber"]', ["e"]),
        (f'{TAGGED}:tags[value ne "red"]', ["a", "b", "c", "e"]),
        (f"{TAGGED}:tags[value pr]", ["a", "b", "e"]),
        (f'{TAGGED}:tags[value sw "r" and value ew "d"]', ["a"]),
        (f'{TAGGED}:tags[not (value eq "green")]', ["a", "c", "e"]),
    ],
)
def test_a_filter_holds_when_one_value_matches(
    storage, user_type, scim_filter, expected
):
    """A comparison on the attribute, or on value in a selection, compares each value, as ScimFilter.match does."""
    assert sorted(user_names(storage, user_type, filter=scim_filter)) == expected


def test_a_selection_on_another_attribute_matches_nothing(storage, user_type):
    """A value holds no sub-attribute, so only value designates it."""
    scim_filter = f'{TAGGED}:tags[display eq "red"] or userName eq "b"'

    assert user_names(storage, user_type, filter=scim_filter) == ["b"]


@pytest.mark.parametrize(
    ("sort_order", "expected"),
    [
        ("ascending", ["c", "e", "b", "a", "d"]),
        ("descending", ["d", "a", "b", "e", "c"]),
    ],
)
def test_a_sort_uses_the_first_value(storage, user_type, sort_order, expected):
    """Per RFC 7644 §3.4.2.3, a multi-valued attribute without primary sorts on its first value."""
    names = user_names(
        storage, user_type, sort_by=f"{TAGGED}:tags", sort_order=sort_order
    )

    assert names == expected


def test_the_schema_keeps_the_values():
    """The values are written, so the attribute keeps its mutability."""
    _, extension = tagged_mapping().schemas()

    (attribute,) = extension.attributes
    assert (attribute.name, attribute.multi_valued) == ("tags", True)
    assert attribute.mutability == Mutability.read_write


def test_read_only_values_are_not_written(factory, user_type):
    """A column the storage only reads makes the values read-only, and a change raises a 400."""
    mapping = tagged_mapping(tags=Attribute(Tag.value, writable=False))
    storage = factory(mapping)
    _, extension = mapping.schemas()

    user = storage.create(user_type, tagged("a", None))
    user[Tagged] = Tagged(tags=["red"])

    assert extension.attributes[0].mutability == Mutability.read_only
    with pytest.raises(MutabilityException):
        storage.update(user_type, user)


def test_write_only_values_are_never_read(factory, user_type):
    """The values are never returned, and filters and sorts refuse them."""
    storage = factory(tagged_mapping(tags=Attribute(Tag.value, readable=False)))

    user = storage.create(user_type, tagged("a", ["red"]))

    assert user[Tagged] is None
    with pytest.raises(InvalidFilterException):
        user_names(storage, user_type, filter=f"{TAGGED}:tags pr")
    with pytest.raises(InvalidFilterException):
        user_names(storage, user_type, filter=f'{TAGGED}:tags[value eq "red"]')
    with pytest.raises(InvalidPathException):
        user_names(storage, user_type, sort_by=f"{TAGGED}:tags")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"userName": Many(TaggedUser.tags, Tag.value)},
            "'userName' is not a multi-valued simple attribute",
        ),
        (
            {"emails": Many(TaggedUser.tags, Tag.value)},
            "'emails' is not a multi-valued simple attribute",
        ),
        (
            {f"{TAGGED}:tags": Many(TaggedUser.tags, {"value": Tag.value})},
            "tags' is not a multi-valued complex attribute",
        ),
        ({f"{TAGGED}:tags": Tag.value}, "map it with Many"),
        (
            {f"{TAGGED}:badge.lines": Tag.value},
            "holds several values in a complex attribute",
        ),
    ],
    ids=["single", "complex", "dict on simple", "column", "in a complex attribute"],
)
def test_many_values_are_mapped_on_a_multi_valued_simple_attribute(changes, message):
    """Many with a column maps the values of a list, which a complex attribute cannot hold."""
    with pytest.raises(ValueError, match=message):
        ResourceMapping(
            User[Tagged],
            TaggedUser,
            {
                "id": TaggedUser.id,
                "meta.created": TaggedUser.created,
                "meta.lastModified": TaggedUser.last_modified,
                **changes,
            },
            version=TaggedUser.version,
        )
