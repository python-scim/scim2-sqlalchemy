import datetime

import pytest
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import Mutability
from scim2_models import User
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.types import UserDefinedType

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Link
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping
from scim2_sqlalchemy import SqlAlchemyStorage
from scim2_sqlalchemy.mapping import _holds_text
from scim2_sqlalchemy.query import _as_key

from .models import ENTERPRISE
from .models import EmailRecord
from .models import GroupRecord
from .models import UserRecord
from .models import groups_mapping
from .models import users_mapping


class Base(DeclarativeBase):
    pass


class Opaque(UserDefinedType[str]):
    """A column type telling no Python type."""

    cache_ok = True


class Person(Base):
    __tablename__ = "people"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_name: Mapped[str]
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()
    other_version: Mapped[int] = mapped_column()
    emails: Mapped[list["Address"]] = relationship()
    opaque: Mapped[str] = mapped_column(Opaque())

    __mapper_args__ = {"version_id_col": version}


class Address(Base):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    value: Mapped[str | None]


REQUIRED = {
    "id": Person.id,
    "meta.created": Person.created,
    "meta.lastModified": Person.last_modified,
}


def person_mapping(**attributes):
    return ResourceMapping(User, Person, REQUIRED | attributes, version=Person.version)


@pytest.mark.parametrize("missing", ["id", "meta.created", "meta.lastModified"])
def test_a_mapping_needs_the_identifier_and_the_dates(missing):
    """The storage fills these attributes, so it needs a column for each."""
    attributes = {key: value for key, value in REQUIRED.items() if key != missing}

    with pytest.raises(ValueError, match=missing):
        ResourceMapping(User, Person, attributes, version=Person.version)


def test_an_unknown_path_is_refused():
    """A typo in a path shows up when the mapping is created."""
    with pytest.raises(ValueError, match="'usrName' is not an attribute of User"):
        person_mapping(usrName=Person.user_name)


def test_a_complex_attribute_is_mapped_by_its_sub_attributes():
    """A column holds one value, not a whole name."""
    with pytest.raises(ValueError, match="map its sub-attributes"):
        person_mapping(name=Person.user_name)


def test_a_multi_valued_attribute_is_not_mapped_to_a_column():
    """A column holds one value, not a list of emails."""
    with pytest.raises(ValueError, match="map it with Many"):
        person_mapping(emails=Person.user_name)


def test_many_maps_a_multi_valued_complex_attribute_only():
    """Many describes entries, which userName has none of."""
    with pytest.raises(ValueError, match="not a multi-valued complex attribute"):
        person_mapping(userName=Many(Person.emails, {"value": Address.value}))


def test_many_needs_the_delete_orphan_cascade():
    """Without it, replacing the entries would leave the former ones behind."""
    with pytest.raises(ValueError, match="delete-orphan"):
        person_mapping(emails=Many(Person.emails, {"value": Address.value}))


def test_an_attribute_is_mapped_to_a_column():
    """A plain value cannot be read from a record."""
    with pytest.raises(ValueError, match="column or a hybrid property"):
        person_mapping(userName="user_name")


def test_the_version_is_the_version_column_of_the_mapper():
    """Another column would not change on each write."""
    with pytest.raises(ValueError, match="version_id_col"):
        ResourceMapping(User, Person, REQUIRED, version=Person.other_version)


def test_references_need_the_mapping_of_their_resource_type():
    """A link to a resource type the storage does not serve cannot be read."""
    groups = ResourceMapping(
        Group,
        GroupRecord,
        {
            "id": GroupRecord.id,
            "members": Link(GroupRecord.members, "User"),
            "meta.created": GroupRecord.created,
            "meta.lastModified": GroupRecord.last_modified,
        },
        version=GroupRecord.version,
    )

    with pytest.raises(ValueError, match="No mapping for the resource type 'User'"):
        SqlAlchemyStorage({"Group": groups}, lambda: None)


def test_a_link_displays_the_user_name_without_a_display_name():
    """A user mapping without displayName still gives its members a display."""
    users = ResourceMapping(
        User,
        UserRecord,
        {
            "id": UserRecord.id,
            "userName": UserRecord.user_name,
            "emails": Many(UserRecord.emails, {"value": EmailRecord.value}),
            "meta.created": UserRecord.created,
            "meta.lastModified": UserRecord.last_modified,
        },
        version=UserRecord.version,
    )

    assert users._display().key == "user_name"


def test_a_link_displays_nothing_without_a_name():
    """A group mapping without displayName gives its links no display."""
    groups = ResourceMapping(
        Group,
        GroupRecord,
        {
            "id": GroupRecord.id,
            "meta.created": GroupRecord.created,
            "meta.lastModified": GroupRecord.last_modified,
        },
        version=GroupRecord.version,
    )

    assert groups._display() is None


def test_a_column_type_without_python_type_holds_text():
    """A column whose type tells no Python type is compared as it is."""
    mapping = person_mapping(nickName=Person.opaque)
    column = mapping._column("nickName")

    assert _holds_text(Person.opaque)
    assert _as_key(column, "x") == "x"


def test_a_mapping_links_to_the_same_mappings_in_every_storage():
    """A mapping shared by two storages cannot link to two different mappings."""
    groups = groups_mapping()
    SqlAlchemyStorage({"User": users_mapping(), "Group": groups}, lambda: None)

    with pytest.raises(ValueError, match="already links to another mapping"):
        SqlAlchemyStorage({"User": users_mapping(), "Group": groups}, lambda: None)


def test_the_schemas_hold_the_mapped_attributes_only():
    """An attribute the mapping does not store is not published."""
    (user, enterprise) = users_mapping().schemas()

    names = {attribute.name for attribute in user.attributes}
    assert {"userName", "emails", "password"} <= names
    assert names.isdisjoint({"phoneNumbers", "profileUrl", "x509Certificates"})
    (name,) = [a for a in user.attributes if a.name == "name"]
    assert [sub.name for sub in name.sub_attributes] == ["familyName", "givenName"]
    assert [a.name for a in enterprise.attributes] == ["employeeNumber", "manager"]


def test_an_attribute_the_mapping_only_reads_is_published_read_only():
    """NickName comes from a hybrid property without setter."""
    (user, _) = users_mapping().schemas()

    (nick_name,) = [a for a in user.attributes if a.name == "nickName"]
    assert nick_name.mutability == Mutability.read_only


def test_a_link_publishes_its_value_its_ref_and_a_read_only_display():
    """A member is published before any storage links the mapping, with its $ref to users only."""
    (group,) = groups_mapping().schemas()

    (members,) = [a for a in group.attributes if a.name == "members"]
    assert [(sub.name, sub.mutability) for sub in members.sub_attributes] == [
        ("value", Mutability.immutable),
        ("$ref", Mutability.immutable),
        ("display", Mutability.read_only),
    ]
    (ref,) = [sub for sub in members.sub_attributes if sub.name == "$ref"]
    assert ref.reference_types == ["User"]


def test_a_complex_attribute_only_read_is_published_read_only():
    """Entries whose every mapped sub-attribute is only read cannot be written."""
    emails = Many(
        UserRecord.emails, {"value": Attribute(EmailRecord.value, writable=False)}
    )
    (user, _) = users_mapping({"emails": emails}).schemas()

    (emails,) = [a for a in user.attributes if a.name == "emails"]
    assert emails.mutability == Mutability.read_only


def test_a_single_link_needs_a_relationship_holding_one_record():
    """The manager is one user, so a relationship holding several records does not fit it."""
    with pytest.raises(
        ValueError, match="holds one entry, but its relationship holds several"
    ):
        ResourceMapping(
            User[EnterpriseUser],
            UserRecord,
            {
                "id": UserRecord.id,
                f"{ENTERPRISE}:manager": Link(UserRecord.groups, "Group"),
                "meta.created": UserRecord.created,
                "meta.lastModified": UserRecord.last_modified,
            },
            version=UserRecord.version,
        )


def test_several_links_need_a_relationship_holding_several_records():
    """The members are several users, so a relationship holding one record does not fit them."""
    with pytest.raises(
        ValueError, match="holds several entries, but its relationship holds one"
    ):
        users_mapping({"groups": Link(UserRecord.manager, "Group")})


def test_a_single_link_publishes_its_value_its_ref_and_a_read_only_display_name():
    """The manager is published with the sub-attributes the storage reads."""
    (_, enterprise) = users_mapping().schemas()

    (manager,) = [a for a in enterprise.attributes if a.name == "manager"]
    assert [(sub.name, sub.mutability) for sub in manager.sub_attributes] == [
        ("value", Mutability.read_write),
        ("$ref", Mutability.read_write),
        ("displayName", Mutability.read_only),
    ]
