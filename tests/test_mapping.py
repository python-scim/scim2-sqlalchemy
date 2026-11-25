import datetime
import warnings
from typing import Annotated

import pytest
from scim2_models import URN
from scim2_models import ComplexAttribute
from scim2_models import EnterpriseUser
from scim2_models import Extension
from scim2_models import Group
from scim2_models import Manager
from scim2_models import Mutability
from scim2_models import Required
from scim2_models import Resource
from scim2_models import Returned
from scim2_models import User
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import cast
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import column_property
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.types import UserDefinedType

from scim2_sqlalchemy import Attribute
from scim2_sqlalchemy import Hashed
from scim2_sqlalchemy import Link
from scim2_sqlalchemy import Many
from scim2_sqlalchemy import ResourceMapping
from scim2_sqlalchemy import SqlAlchemyStorage
from scim2_sqlalchemy.mapping import _holds_text
from scim2_sqlalchemy.query import _as_key

from .models import ENTERPRISE
from .models import TYPES
from .models import CertificateRecord
from .models import EmailRecord
from .models import GroupRecord
from .models import UserRecord
from .models import groups_mapping
from .models import users_mapping


class Base(DeclarativeBase):
    type_annotation_map = TYPES


class Opaque(UserDefinedType[str]):
    """A column type telling no Python type."""

    cache_ok = True


class Label(str):
    """A string of its own kind."""


class LabelType(UserDefinedType[str]):
    """A column type holding Label strings."""

    cache_ok = True

    @property
    def python_type(self) -> type:
        """Tell the Python type of the values."""
        return Label


class Unknown(UserDefinedType[str]):
    """A column type refusing to tell its Python type, as SQLAlchemy 2.0 types do."""

    cache_ok = True

    @property
    def python_type(self) -> type:
        """Refuse to tell the Python type."""
        raise NotImplementedError


class Person(Base):
    __tablename__ = "people"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="1")
    id_text: Mapped[str] = column_property(cast(id, String))
    user_name: Mapped[str | None]
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()
    other_version: Mapped[int | None] = mapped_column()
    emails: Mapped[list["Address"]] = relationship()
    desks: Mapped[list["Desk"]] = relationship(
        foreign_keys="Desk.owner_id", back_populates="owner"
    )
    opaque: Mapped[str | None] = mapped_column(Opaque())
    unknown: Mapped[str | None] = mapped_column(Unknown())
    label: Mapped[str | None] = mapped_column(LabelType())
    secret: Mapped[str | None] = mapped_column(Hashed(str.upper))

    __mapper_args__ = {"version_id_col": version}


class Address(Base):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    value: Mapped[str | None]


class Desk(Base):
    """A desk, whose owner and keeper cannot be missing."""

    __tablename__ = "desks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="1")
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()
    owner_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    owner: Mapped[Person] = relationship(
        foreign_keys=[owner_id], back_populates="desks"
    )
    keeper_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    keeper: Mapped[Person] = relationship(foreign_keys=[keeper_id])

    __mapper_args__ = {"version_id_col": version}


OWNED = "urn:example:params:scim:schemas:extension:owned:2.0:User"


class Owned(Extension):
    __schema__ = URN(OWNED)

    owner: Annotated[Manager | None, Required.true] = None
    keeper: Annotated[Manager | None, Mutability.read_only] = None


def desk_mapping(model, attributes):
    return ResourceMapping(
        model,
        Desk,
        {
            "id": Desk.id,
            "meta.created": Desk.created,
            "meta.lastModified": Desk.last_modified,
            **attributes,
        },
        version=Desk.version,
    )


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


def test_an_identifier_that_can_be_missing_is_refused():
    """A resource without id could not be read back, nor placed between two cursor pages."""
    with pytest.raises(
        ValueError, match="'id' is mapped to a column that can hold no value"
    ):
        users_mapping({"id": UserRecord.external_id, "externalId": None})


def test_a_hash_holds_a_write_only_attribute_only():
    """The schema makes password write-only, so its hash needs no readable=False, unlike nickName."""
    person_mapping(password=Person.secret)

    with pytest.raises(ValueError, match="'nickName' stores a hash"):
        person_mapping(nickName=Person.secret)


@pytest.mark.parametrize(
    "path, value",
    [
        ("title", Attribute(UserRecord.title, readable=False, writable=False)),
        ("password", UserRecord.lower_name),
    ],
    ids=["Attribute", "write-only property without setter"],
)
def test_an_attribute_neither_read_nor_written_is_refused(path, value):
    """The storage would do nothing with it, yet publish it."""
    with pytest.raises(ValueError, match="neither read nor written"):
        users_mapping({path: value})


def test_a_link_that_can_be_removed_needs_a_foreign_key_accepting_null():
    """Removing the manager would write NULL in a column that refuses it."""
    with pytest.raises(ValueError, match="foreign key of its relationship must"):
        desk_mapping(
            User[EnterpriseUser],
            {
                f"{ENTERPRISE}:manager": Link(Desk.owner, "User"),
                "userName": Desk.keeper_id,
            },
        )


def test_a_required_link_of_an_optional_extension_can_be_removed():
    """A resource without the extension has no owner, so the foreign key must accept NULL."""
    with pytest.raises(ValueError, match="foreign key of its relationship must"):
        desk_mapping(
            User[Owned],
            {
                f"{OWNED}:owner": Link(Desk.owner, "User"),
                f"{OWNED}:keeper": Link(Desk.keeper, "User"),
            },
        )


class DeskResource(Resource):
    __schema__ = URN("urn:example:params:scim:schemas:core:2.0:Desk")

    owner: Annotated[Manager | None, Required.true] = None
    keeper: Annotated[Manager | None, Required.true] = None


def test_a_required_link_needs_no_foreign_key_accepting_null():
    """Every desk has an owner and a keeper, so the links are never removed."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        desk_mapping(
            DeskResource,
            {
                "owner": Link(Desk.owner, "User"),
                "keeper": Link(Desk.keeper, "User"),
            },
        )


def test_a_link_to_records_whose_foreign_key_cannot_be_null_is_refused():
    """Removing a member would write NULL in the owner of its desk."""
    with pytest.raises(ValueError, match="writes NULL in the foreign key"):
        ResourceMapping(
            Group,
            Person,
            REQUIRED | {"members": Link(Person.desks, "User")},
            version=Person.version,
        )


def test_a_column_the_mapping_never_writes_warns():
    """The keeper is read-only, so a new desk could not be stored, unlike its required owner."""
    with pytest.warns(UserWarning) as caught:
        desk_mapping(
            User[Annotated[Owned, Required.true]],
            {
                f"{OWNED}:owner": Link(Desk.owner, "User"),
                f"{OWNED}:keeper": Link(Desk.keeper, "User"),
            },
        )

    assert [str(warning.message) for warning in caught] == [
        "The column desks.keeper_id needs a value, but the mapping of "
        "User[Owned] never writes it: map it, or give it a default"
    ]


@pytest.mark.parametrize(
    "user_name",
    [None, Attribute(UserRecord.user_name, writable=False)],
    ids=["unmapped", "read-only"],
)
def test_a_column_needing_a_value_warns_when_it_is_not_written(user_name):
    """Creating a user would fail in the database."""
    with pytest.warns(UserWarning, match="users.user_name needs a value"):
        users_mapping({"userName": user_name})


class Party(Base):
    """A party, whose kind tells its class apart."""

    __tablename__ = "parties"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column()
    created: Mapped[datetime.datetime]
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()

    __mapper_args__ = {"polymorphic_on": kind, "version_id_col": version}


class Member(Party):
    """A party with a login, written through a property over two columns."""

    __tablename__ = "members"

    id: Mapped[int] = mapped_column(ForeignKey("parties.id"), primary_key=True)
    first: Mapped[str] = mapped_column()
    last: Mapped[str] = mapped_column()

    __mapper_args__ = {"polymorphic_identity": "member"}

    @hybrid_property
    def login(self) -> str:
        return f"{self.first}.{self.last}"

    @login.inplace.setter
    def _login_setter(self, value: str) -> None:
        self.first, self.last = value.split(".")

    @login.inplace.expression
    @classmethod
    def _login_expression(cls):
        return cls.first + "." + cls.last

    first_text: Mapped[str] = column_property(cast(first, String))


class Leader(Member):
    """A member two joined tables away from the parties."""

    __tablename__ = "leaders"

    id: Mapped[int] = mapped_column(ForeignKey("members.id"), primary_key=True)

    __mapper_args__ = {"polymorphic_identity": "leader"}


def leader_mapping(user_name):
    return ResourceMapping(
        User,
        Leader,
        {
            "id": Leader.id,
            "userName": user_name,
            "meta.created": Leader.created,
            "meta.lastModified": Leader.last_modified,
        },
        version=Leader.version,
    )


def test_the_columns_sqlalchemy_or_a_setter_fills_need_no_mapping():
    """The class discriminator, the inherited keys and the columns of the login setter are written."""
    leader = Leader(login="ada.lovelace")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        leader_mapping(Leader.login)
    assert (leader.first, leader.last, leader.login) == (
        "ada",
        "lovelace",
        "ada.lovelace",
    )


class Stamped(Base):
    """A record whose creation date is written through a property."""

    __tablename__ = "stamped"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column()
    last_modified: Mapped[datetime.datetime]
    version: Mapped[int] = mapped_column()

    __mapper_args__ = {"version_id_col": version}

    @hybrid_property
    def created(self) -> datetime.datetime:
        return self.created_at

    @created.inplace.setter
    def _created_setter(self, value: datetime.datetime) -> None:
        self.created_at = value


def test_the_dates_written_through_a_property_need_no_mapping():
    """The storage sets the creation date, so the column behind its property is written."""
    now = datetime.datetime.now(datetime.UTC)
    stamped = Stamped(created=now)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ResourceMapping(
            Group,
            Stamped,
            {
                "id": Stamped.id,
                "meta.created": Stamped.created,
                "meta.lastModified": Stamped.last_modified,
            },
            version=Stamped.version,
        )
    assert stamped.created == now


def test_a_column_property_writes_no_column():
    """Setting a column property writes nothing, so the first and last names are never written."""
    with pytest.warns(UserWarning) as caught:
        leader_mapping(Leader.first_text)

    assert [str(warning.message).split(" needs")[0] for warning in caught] == [
        "The column members.first",
        "The column members.last",
    ]


def test_the_columns_the_storage_fills_need_no_mapping():
    """The dates, the version and the numbered key are filled without a written attribute."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        users_mapping()
        groups_mapping()


def test_an_identifier_can_be_a_column_property():
    """A column property over a SQL expression tells nothing of its values being missing."""
    mapping = person_mapping(id=Person.id_text)

    assert mapping._id.key == "id_text"


SECRET = "urn:example:params:scim:schemas:extension:secret:2.0:User"


class Secret(ComplexAttribute):
    value: Annotated[str | None, Mutability.write_only] = None


class Secrets(Extension):
    __schema__ = URN(SECRET)

    secrets: list[Secret] | None = None
    codes: Annotated[list[str] | None, Mutability.write_only] = None


@pytest.mark.parametrize(
    ("path", "attributes"),
    [("secrets", {"value": EmailRecord.value}), ("codes", EmailRecord.value)],
    ids=["sub-attribute", "attribute"],
)
def test_entries_the_schema_makes_write_only_are_refused(path, attributes):
    """A client never reads them back, so a write would lose them."""
    with pytest.raises(ValueError, match="cannot hold write-only values"):
        ResourceMapping(
            User[Secrets],
            UserRecord,
            {
                "id": UserRecord.id,
                "meta.created": UserRecord.created,
                "meta.lastModified": UserRecord.last_modified,
                f"{SECRET}:{path}": Many(UserRecord.emails, attributes),
            },
            version=UserRecord.version,
        )


def test_an_unknown_path_is_refused():
    """A typo in a path shows up when the mapping is created."""
    with pytest.raises(ValueError, match="'usrName' is not an attribute of User"):
        person_mapping(usrName=Person.user_name)


@pytest.mark.parametrize(
    "path", ["ID", "urn:ietf:params:scim:schemas:core:2.0:User:id"]
)
def test_a_path_is_case_insensitive_and_may_be_a_urn(path):
    """Per RFC 7643 §2.1, attribute names are case insensitive."""
    attributes = {key: value for key, value in REQUIRED.items() if key != "id"}
    mapping = ResourceMapping(
        User, Person, {path: Person.id, **attributes}, version=Person.version
    )

    assert mapping._column("id").key == "id"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"username": UserRecord.title}, "'userName' and 'username'"),
        (
            {"urn:ietf:params:scim:schemas:core:2.0:User:title": UserRecord.title},
            "'title' and 'urn:ietf:params:scim:schemas:core:2.0:User:title'",
        ),
        (
            {"Emails": Many(UserRecord.emails, {"value": EmailRecord.value})},
            "'emails' and 'Emails'",
        ),
        (
            {
                "emails": Many(
                    UserRecord.emails,
                    {"value": EmailRecord.value, "Value": EmailRecord.type},
                )
            },
            "'emails.value' and 'emails.Value'",
        ),
        (
            {f"{ENTERPRISE}:manager.value": UserRecord.manager_id},
            f"'{ENTERPRISE}:manager' and '{ENTERPRISE}:manager.value'",
        ),
        (
            {"name": Link(UserRecord.manager, "User")},
            "'name.givenName' and 'name'",
        ),
    ],
    ids=["case", "urn", "collection", "entry", "sub-attribute", "parent"],
)
def test_two_paths_to_the_same_attribute_are_refused(changes, message):
    """Two paths to one attribute would leave one of them unused."""
    with pytest.raises(ValueError, match=f"{message} map the same attribute"):
        users_mapping(changes)


@pytest.mark.parametrize(
    "emails, message",
    [
        (
            {
                "value": EmailRecord.value,
                "type": Attribute(EmailRecord.type, readable=False),
            },
            "cannot hold write-only values",
        ),
        (
            {
                "value": Attribute(EmailRecord.value, readable=False),
                "type": Attribute(EmailRecord.type, readable=False),
            },
            "cannot hold write-only values",
        ),
        (
            {
                "value": EmailRecord.value,
                "display": Attribute(EmailRecord.value, writable=False),
            },
            "all written or all read-only",
        ),
    ],
    ids=["write-only", "all write-only", "read-only"],
)
def test_the_entries_of_a_many_are_read_and_written_alike(emails, message):
    """Writing the emails replaces them, which would lose a value the client never sends back."""
    with pytest.raises(
        ValueError, match=f"Writing 'emails' replaces its entries, so .* {message}"
    ):
        users_mapping({"emails": Many(UserRecord.emails, emails)})


@pytest.mark.parametrize(
    "changes, path",
    [
        ({"nickName": UserRecord.version}, "nickName"),
        ({"externalId": UserRecord.manager_id}, "externalId"),
    ],
    ids=["integer", "uuid"],
)
def test_a_string_is_not_mapped_to_a_column_of_another_type(changes, path):
    """Reading the user would fail on a value that is not a string."""
    with pytest.raises(ValueError, match=f"'{path}' holds strings, but its column"):
        users_mapping(changes)


def test_a_string_is_mapped_to_a_column_of_a_kind_of_string():
    """A column type may hold a subclass of str."""
    mapping = person_mapping(nickName=Person.label)

    assert mapping._column("nickName").key == "label"


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


def test_link_maps_a_complex_attribute_only():
    """A Link gives a value and a display, which userName has no room for."""
    with pytest.raises(ValueError, match="not a complex attribute"):
        person_mapping(userName=Link(Person.emails, "User"))


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


@pytest.mark.parametrize("record_column", ["opaque", "unknown"])
def test_a_column_type_without_python_type_holds_text(record_column):
    """A column whose type tells no Python type is compared as it is."""
    expression = getattr(Person, record_column)
    mapping = person_mapping(nickName=expression)
    column = mapping._column("nickName")

    assert _holds_text(expression)
    assert _as_key(column, "x") == "x"


def test_a_mapping_links_to_the_same_mappings_in_every_storage():
    """A mapping shared by two storages cannot link to two different mappings."""
    groups = groups_mapping()
    SqlAlchemyStorage({"User": users_mapping(), "Group": groups}, lambda: None)

    with pytest.raises(ValueError, match="already links to another mapping"):
        SqlAlchemyStorage({"User": users_mapping(), "Group": groups}, lambda: None)


@pytest.mark.parametrize("refused", ["the first mapping", "a later mapping"])
def test_a_storage_refused_for_a_missing_mapping_links_nothing(refused):
    """A refused storage binds no Link, so its mappings still fit a corrected storage."""
    users, groups = users_mapping(), groups_mapping()
    if refused == "the first mapping":
        mappings = {"Users": users, "Group": groups_mapping()}
    else:
        teams = groups_mapping({"members": Link(GroupRecord.members, "Member")})
        mappings = {"Group": groups, "User": users_mapping(), "Team": teams}
    with pytest.raises(ValueError, match="No mapping for the resource type"):
        SqlAlchemyStorage(mappings, lambda: None)

    SqlAlchemyStorage({"User": users, "Group": groups}, lambda: None)


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
    """Locale comes from a hybrid property without setter."""
    (user, _) = users_mapping().schemas()

    (locale,) = [a for a in user.attributes if a.name == "locale"]
    assert locale.mutability == Mutability.read_only


def test_an_attribute_whose_column_needs_a_value_is_published_required():
    """A written attribute is required when its column cannot be NULL and has no default."""
    mapping = users_mapping(
        {
            "nickName": UserRecord.user_name,
            "profileUrl": UserRecord.profile_url,
            "title": Attribute(UserRecord.user_name, writable=False),
            f"{ENTERPRISE}:employeeNumber": UserRecord.user_name,
        }
    )
    (user, enterprise) = mapping.schemas()

    required = {a.name: a.required for a in user.attributes}
    assert required["nickName"] == Required.true
    assert required["profileUrl"] == Required.false
    assert required["title"] == Required.false
    (employee_number, _) = enterprise.attributes
    assert employee_number.required == Required.true


@pytest.mark.parametrize(
    "password",
    [Attribute(UserRecord.user_name, readable=False), UserRecord.user_name],
    ids=["readable=False", "writeOnly in the schema"],
)
def test_a_write_only_attribute_is_not_published_required(password):
    """A replacement may leave out a write-only value, which the storage keeps."""
    (user, _) = users_mapping({"password": password}).schemas()

    (attribute,) = [a for a in user.attributes if a.name == "password"]
    assert attribute.required == Required.false


HIDDEN = "urn:example:params:scim:schemas:extension:hidden:2.0:User"


class Card(ComplexAttribute):
    number: str | None = None


class Hidden(Extension):
    __schema__ = URN(HIDDEN)

    token: Annotated[str | None, Returned.never] = None
    badge: Annotated[str | None, Mutability.immutable, Required.true] = None
    card: Annotated[Card | None, Mutability.immutable, Required.true] = None
    serial: Annotated[str | None, Mutability.immutable, Returned.never] = None


def test_an_attribute_never_returned_is_write_only():
    """A client never reads the token back, so the storage neither reads it nor requires it."""
    mapping = ResourceMapping(
        User[Hidden],
        UserRecord,
        {
            "id": UserRecord.id,
            "userName": UserRecord.user_name,
            f"{HIDDEN}:token": UserRecord.user_name,
            "meta.created": UserRecord.created,
            "meta.lastModified": UserRecord.last_modified,
        },
        version=UserRecord.version,
    )
    (_, hidden) = mapping.schemas()

    (token,) = hidden.attributes
    assert token.required == Required.false
    assert not mapping._column(f"{HIDDEN}:token").readable


@pytest.mark.parametrize(
    "family_name, expected",
    [
        (
            Attribute(UserRecord.family_name, readable=False),
            (Mutability.write_only, Returned.never),
        ),
        (UserRecord.family_name, (Mutability.read_write, Returned.default)),
    ],
    ids=["no sub-attribute returned", "one sub-attribute returned"],
)
def test_a_complex_attribute_is_returned_when_one_of_its_sub_attributes_is(
    family_name, expected
):
    """A name whose parts are never returned is never returned either."""
    given_name = Attribute(UserRecord.given_name, readable=False)
    mapping = users_mapping(
        {"name.givenName": given_name, "name.familyName": family_name}
    )
    (user, _) = mapping.schemas()

    (name,) = [a for a in user.attributes if a.name == "name"]
    assert (name.mutability, name.returned) == expected


@pytest.mark.parametrize("path", ["badge", "card.number"])
def test_an_immutable_attribute_the_mapping_only_writes_stays_immutable(path):
    """A client still sets the badge or the card once, and never reads it back."""
    mapping = ResourceMapping(
        User[Hidden],
        UserRecord,
        {
            "id": UserRecord.id,
            "userName": UserRecord.user_name,
            f"{HIDDEN}:{path}": Attribute(UserRecord.title, readable=False),
            "meta.created": UserRecord.created,
            "meta.lastModified": UserRecord.last_modified,
        },
        version=UserRecord.version,
    )
    (_, hidden) = mapping.schemas()

    (attribute,) = hidden.attributes
    assert (attribute.mutability, attribute.returned, attribute.required) == (
        Mutability.immutable,
        Returned.never,
        Required.false,
    )


def test_an_immutable_value_never_returned_is_still_read():
    """The models check that the serial never changes against the stored one."""
    mapping = ResourceMapping(
        User[Hidden],
        UserRecord,
        {
            "id": UserRecord.id,
            "userName": UserRecord.user_name,
            f"{HIDDEN}:serial": UserRecord.title,
            "meta.created": UserRecord.created,
            "meta.lastModified": UserRecord.last_modified,
        },
        version=UserRecord.version,
    )

    assert mapping._column(f"{HIDDEN}:serial").readable


def test_a_complex_attribute_whose_sub_attribute_needs_a_value_is_published_required():
    """A new resource without name has no givenName, so name is required with it."""
    (user, _) = users_mapping({"name.givenName": UserRecord.user_name}).schemas()

    (name,) = [a for a in user.attributes if a.name == "name"]
    assert name.required == Required.true
    assert {sub.name: sub.required for sub in name.sub_attributes} == {
        "familyName": Required.false,
        "givenName": Required.true,
    }


def test_a_complex_attribute_with_a_read_only_sub_attribute_is_not_required():
    """The storage never writes a read-only givenName, so a new resource needs no name."""
    given_name = Attribute(UserRecord.user_name, writable=False)
    (user, _) = users_mapping({"name.givenName": given_name}).schemas()

    (name,) = [a for a in user.attributes if a.name == "name"]
    assert name.required == Required.false


def test_an_entry_whose_column_needs_a_value_is_published_required():
    """Each certificate needs a value, but a user may have no certificate."""
    certificates = Many(
        UserRecord.certificates,
        {"value": CertificateRecord.value, "type": CertificateRecord.type},
    )
    (user, _) = users_mapping({"x509Certificates": certificates}).schemas()

    (attribute,) = [a for a in user.attributes if a.name == "x509Certificates"]
    assert attribute.required == Required.false
    assert {sub.name: sub.required for sub in attribute.sub_attributes} == {
        "value": Required.true,
        "type": Required.false,
    }


def test_an_attribute_the_mapping_only_writes_is_published_write_only():
    """The storage never returns the title, so clients cannot read it back."""
    title = Attribute(UserRecord.title, readable=False)
    (user, _) = users_mapping({"title": title}).schemas()

    (attribute,) = [a for a in user.attributes if a.name == "title"]
    assert attribute.mutability == Mutability.write_only
    assert attribute.returned == Returned.never


def test_a_link_publishes_its_value_its_ref_its_type_and_a_read_only_display():
    """A member is published before any storage links the mapping, with its $ref to the resource types of its Links."""
    (group,) = groups_mapping().schemas()

    (members,) = [a for a in group.attributes if a.name == "members"]
    assert [(sub.name, sub.mutability) for sub in members.sub_attributes] == [
        ("value", Mutability.immutable),
        ("$ref", Mutability.immutable),
        ("type", Mutability.immutable),
        ("display", Mutability.read_only),
    ]
    (ref,) = [sub for sub in members.sub_attributes if sub.name == "$ref"]
    assert ref.reference_types == ["User", "Group"]


def test_a_link_without_type_does_not_publish_the_type():
    """The type of a member is published only when its Links give one."""
    (group,) = groups_mapping({"members": Link(GroupRecord.members, "User")}).schemas()

    (members,) = [a for a in group.attributes if a.name == "members"]
    assert "type" not in [sub.name for sub in members.sub_attributes]
    (ref,) = [sub for sub in members.sub_attributes if sub.name == "$ref"]
    assert ref.reference_types == ["User"]


@pytest.mark.parametrize(
    "links, message",
    [
        ([], "at least one"),
        (
            [Link(GroupRecord.members, "User"), Link(GroupRecord.subgroups, "User")],
            "one Link per resource type",
        ),
        (
            [
                Link(GroupRecord.members, "User", type="User"),
                Link(GroupRecord.subgroups, "Group"),
            ],
            "every Link of 'members' has a type, or none",
        ),
        ([Link(GroupRecord.members, "User"), "User"], "map it with Many or Link"),
    ],
    ids=["empty", "same resource type", "type on some", "not a Link"],
)
def test_the_links_of_an_attribute_are_checked(links, message):
    """The Links of an attribute link to distinct resource types, and give a type all or none."""
    with pytest.raises(ValueError, match=message):
        groups_mapping({"members": links})


def test_a_single_valued_attribute_is_mapped_to_one_link():
    """The manager is one resource, so it cannot link to users and groups."""
    with pytest.raises(
        ValueError, match="holds one value, so it is mapped to one Link"
    ):
        users_mapping(
            {
                f"{ENTERPRISE}:manager": [
                    Link(UserRecord.manager, "User"),
                    Link(UserRecord.manager, "Group"),
                ]
            }
        )


def test_a_link_type_needs_a_type_sub_attribute():
    """The manager has no type sub-attribute, so its Link cannot give one."""
    with pytest.raises(ValueError, match="has no type sub-attribute"):
        users_mapping(
            {f"{ENTERPRISE}:manager": Link(UserRecord.manager, "User", type="User")}
        )


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
