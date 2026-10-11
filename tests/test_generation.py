from typing import Annotated

import pytest
from pydantic import Field
from scim2_client.engines.wsgi import WSGISCIMClient
from scim2_models import URN
from scim2_models import ComplexAttribute
from scim2_models import EnterpriseUser
from scim2_models import Extension
from scim2_models import Group
from scim2_models import Mutability
from scim2_models import Reference
from scim2_models import Resource
from scim2_models import ResourceType
from scim2_models import ScimProvider
from scim2_models import User
from scim2_server.applications.wsgi import WSGIApplication
from scim2_server.utils import load_default_provider
from scim2_server.utils import load_default_service_provider_config
from scim2_tester import Status
from scim2_tester import check_server
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import LargeBinary
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import create_engine
from sqlalchemy import select

from scim2_sqlalchemy import Hashed
from scim2_sqlalchemy import generate_mappings

ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
STAFFING = "urn:example:params:scim:schemas:extension:staffing:2.0:User"
PROJECT = "urn:example:params:scim:schemas:core:2.0:Project"
PASSWORD = "urn:ietf:params:scim:schemas:core:2.0:User:password"


def hash_password(password):
    """Mark a password as hashed, so that a test can tell it from the clear text."""
    return f"hashed:{password}"


def hashing_passwords(binding):
    """Store the password hashed, and the other attributes as they are."""
    return Hashed(hash_password) if binding.urn == PASSWORD else True


def hashing_secrets(binding):
    """Store each write-only string hashed, and the other attributes as they are."""
    if binding.get_annotation(Mutability) == Mutability.write_only:
        return Hashed(hash_password)
    return True


def without_secrets(binding):
    """Store every attribute but the write-only strings."""
    return binding.get_annotation(Mutability) != Mutability.write_only


def create_tables(database_url, metadata):
    """Create the tables of metadata, after dropping those another test generated."""
    engine = create_engine(database_url)
    generated = MetaData()
    generated.reflect(engine, only=lambda name, _: name.startswith("generated_"))
    generated.drop_all(engine)
    metadata.create_all(engine)
    engine.dispose()


def publishing(provider, mappings):
    """Return a provider publishing the schemas the mappings store."""
    return ScimProvider.from_discovery(
        [schema for mapping in mappings.values() for schema in mapping.schemas()],
        provider.resource_types,
        config=load_default_service_provider_config(),
    )


@pytest.fixture
def generated(database_url):
    """Return the mappings generated for the default resource types, over empty tables."""
    metadata = MetaData()
    mappings = generate_mappings(
        load_default_provider(),
        metadata,
        prefix="generated_",
        store=hashing_passwords,
    )
    create_tables(database_url, metadata)
    return mappings


@pytest.fixture
def storage(storage_factory, generated):
    """Return a storage serving the generated mappings, with the schemas they publish."""
    return storage_factory(
        mappings=generated, provider=publishing(load_default_provider(), generated)
    )


def test_a_user_is_stored_with_every_kind_of_attribute(storage, user_type):
    """Columns, flattened sub-attributes, child tables, binary values, an extension and a Link store a whole user."""
    boss = storage.create(user_type, User[EnterpriseUser](user_name="boss"))
    user = User[EnterpriseUser].model_validate(
        {
            "userName": "bjensen",
            "name": {"givenName": "Barbara", "familyName": "Jensen"},
            "active": True,
            "emails": [
                {"value": "bjensen@example.com", "type": "work", "primary": True},
                {"value": "babs@example.com", "type": "home"},
            ],
            "x509Certificates": [{"value": "TUlJRA=="}],
            ENTERPRISE: {"employeeNumber": "701984", "manager": {"value": boss.id}},
        }
    )

    created = storage.create(user_type, user).model_dump()

    assert created["name"] == {"givenName": "Barbara", "familyName": "Jensen"}
    assert created["active"] is True
    assert [email["value"] for email in created["emails"]] == [
        "bjensen@example.com",
        "babs@example.com",
    ]
    assert created["x509Certificates"] == [{"value": "TUlJRA=="}]
    assert created[ENTERPRISE]["employeeNumber"] == "701984"
    assert created[ENTERPRISE]["manager"]["value"] == boss.id


def test_a_group_holds_users_and_groups(storage, user_type, group_type):
    """Members links to both resource types, with their type, and groups of a user reads the direct groups."""
    alice = storage.create(user_type, User[EnterpriseUser](user_name="alice"))
    admins = storage.create(
        group_type, Group(display_name="admins", members=[{"value": alice.id}])
    )

    staff = storage.create(
        group_type,
        Group(
            display_name="staff", members=[{"value": admins.id}, {"value": alice.id}]
        ),
    )

    assert [(m.value, m.type) for m in staff.members] == [
        (alice.id, "User"),
        (admins.id, "Group"),
    ]
    groups = storage.get(user_type, alice.id).groups
    assert sorted((g.display, g.type) for g in groups) == [
        ("admins", "direct"),
        ("staff", "direct"),
    ]


def test_the_password_is_stored_hashed(
    sync_storage_factory, session, generated, user_type
):
    """The column holds what the hash function returns, and the user never shows it."""
    storage = sync_storage_factory(mappings=generated)
    user = storage.create(
        user_type, User[EnterpriseUser](user_name="alice", password="secret")
    )
    record = generated["User"].record

    assert session.scalar(select(record.password)) == "hashed:secret"
    assert user.password is None


def test_two_users_can_manage_each_other(storage, user_type):
    """A cycle of managers is stored, although each user needs the other one first."""
    alice = storage.create(user_type, User[EnterpriseUser](user_name="alice"))
    bob = storage.create(
        user_type,
        User[EnterpriseUser].model_validate(
            {"userName": "bob", ENTERPRISE: {"manager": {"value": alice.id}}}
        ),
    )
    alice = User[EnterpriseUser].model_validate(
        {**alice.model_dump(), ENTERPRISE: {"manager": {"value": bob.id}}}
    )

    alice = storage.update(user_type, alice).model_dump()

    assert alice[ENTERPRISE]["manager"]["value"] == bob.id
    bob = storage.get(user_type, bob.id).model_dump()
    assert bob[ENTERPRISE]["manager"]["value"] == alice["id"]


def test_a_generated_server_passes_scim2_tester(sync_storage_factory, generated):
    """A server on generated tables passes every check it announces."""
    provider = publishing(load_default_provider(), generated)
    storage = sync_storage_factory(mappings=generated, provider=provider)
    client = WSGISCIMClient(
        WSGIApplication(storage, provider),
        base_url="http://localhost/v2",
        provider=provider,
    )

    results = check_server(client)

    failures = [
        (result.title, result.reason)
        for result in results
        if result.status not in (Status.SKIPPED, Status.SUCCESS)
    ]
    assert failures == []


def test_the_tables_are_named_after_the_endpoints():
    """Each resource type gets a table, each multi-valued attribute a child or an association table."""
    metadata = MetaData()
    generate_mappings(
        load_default_provider(), metadata, prefix="scim_", store=without_secrets
    )

    assert sorted(metadata.tables) == [
        "scim_groups",
        "scim_groups_members_groups",
        "scim_groups_members_users",
        "scim_users",
        "scim_users_addresses",
        "scim_users_emails",
        "scim_users_entitlements",
        "scim_users_ims",
        "scim_users_phone_numbers",
        "scim_users_photos",
        "scim_users_roles",
        "scim_users_x509_certificates",
    ]


def test_the_columns_follow_the_attributes():
    """A column takes the snake case path of its attribute, and the type, the uniqueness and the requirement of the attribute."""
    metadata = MetaData()
    generate_mappings(load_default_provider(), metadata, store=without_secrets)
    users = metadata.tables["users"].c

    assert not users.user_name.nullable and users.user_name.unique
    assert users.name_given_name.nullable
    assert isinstance(users.active.type, Boolean)
    assert isinstance(users.meta_created.type, DateTime)
    assert users.meta_created.type.timezone
    assert users.enterprise_user_employee_number.nullable
    assert "enterprise_user_manager_id" in users
    assert isinstance(
        metadata.tables["users_x509_certificates"].c.value.type, LargeBinary
    )
    assert not metadata.tables["groups"].c.display_name.nullable


def test_a_write_only_string_needs_a_decision():
    """Storing a password in clear text is never a default: the caller hashes it, or does not store it."""
    with pytest.raises(ValueError, match=f"'{PASSWORD}' are write-only"):
        generate_mappings(load_default_provider(), MetaData())


def test_a_write_only_string_is_never_stored_in_clear_text():
    """Store keeps the password hashed, or not at all."""
    with pytest.raises(ValueError, match=f"'{PASSWORD}' are write-only"):
        generate_mappings(
            load_default_provider(), MetaData(), store=lambda binding: True
        )


def test_a_write_only_sub_attribute_is_never_stored_in_clear_text():
    """Store is asked about a sub-attribute by its full path."""
    token = f"{PROJECT}:milestones.token"

    with pytest.raises(ValueError, match=f"'{token}' are write-only"):
        generate_mappings(
            project_provider(),
            MetaData(),
            store=lambda binding: binding.urn == token or hashing_secrets(binding),
        )


def test_write_only_strings_are_left_out_on_request():
    """Without a hash, the password has no column, and the published schema drops it."""
    metadata = MetaData()
    mappings = generate_mappings(
        load_default_provider(), metadata, store=without_secrets
    )

    (core, _) = mappings["User"].schemas()
    assert "password" not in {attribute.name for attribute in core.attributes}
    assert "password" not in metadata.tables["users"].c


def test_any_attribute_can_be_left_out():
    """A simple, a complex, a multi-valued and a linking attribute are left out of the tables and of the schemas."""
    left_out = {
        "urn:ietf:params:scim:schemas:core:2.0:User:nickName",
        "urn:ietf:params:scim:schemas:core:2.0:User:name",
        "urn:ietf:params:scim:schemas:core:2.0:User:photos",
        "urn:ietf:params:scim:schemas:core:2.0:Group:members",
    }
    metadata = MetaData()

    mappings = generate_mappings(
        load_default_provider(),
        metadata,
        store=lambda binding: (
            binding.urn not in left_out and hashing_passwords(binding)
        ),
    )

    users = metadata.tables["users"].c
    assert not {"nick_name", "name_given_name"} & set(users.keys())
    assert "users_photos" not in metadata.tables
    assert "groups_members_users" not in metadata.tables
    (core, _) = mappings["User"].schemas()
    names = {attribute.name for attribute in core.attributes}
    assert not {"nickName", "name", "photos", "groups"} & names


def test_an_attribute_can_get_its_own_column_type():
    """A simple attribute and the values of a list take the column type store gives."""
    tags = f"{PROJECT}:tags"
    metadata = MetaData()

    generate_mappings(
        project_provider(),
        metadata,
        store=lambda binding: (
            String(20) if binding.urn == tags else hashing_secrets(binding)
        ),
    )

    assert metadata.tables["projects_tags"].c.value.type.length == 20
    assert isinstance(metadata.tables["projects"].c.secret.type, Hashed)


@pytest.mark.parametrize(
    "urn",
    [
        "urn:ietf:params:scim:schemas:core:2.0:User:name",
        "urn:ietf:params:scim:schemas:core:2.0:User:emails",
        "urn:ietf:params:scim:schemas:core:2.0:Group:members",
        "urn:ietf:params:scim:schemas:core:2.0:User:groups",
    ],
    ids=["complex", "multi-valued complex", "link", "inverse"],
)
def test_an_attribute_without_a_column_takes_no_column_type(urn):
    """Only an attribute with a column of its own can be given a column type."""
    with pytest.raises(ValueError, match=f"'{urn}' has no column of its own"):
        generate_mappings(
            load_default_provider(),
            MetaData(),
            store=lambda binding: (
                String() if binding.urn == urn else hashing_passwords(binding)
            ),
        )


def test_a_readable_attribute_cannot_be_hashed():
    """The storage would return the hash of the user name instead of the user name."""
    user_name = "urn:ietf:params:scim:schemas:core:2.0:User:userName"

    with pytest.raises(ValueError, match="'userName' stores a hash"):
        generate_mappings(
            load_default_provider(),
            MetaData(),
            store=lambda binding: (
                Hashed(hash_password)
                if binding.urn == user_name
                else hashing_passwords(binding)
            ),
        )


class ProjectMember(ComplexAttribute):
    """A member of a project, linking to a user."""

    value: str | None = None
    ref: Reference["User"] | None = Field(
        None, serialization_alias="$ref", validation_alias="$ref"
    )
    display: str | None = None


class Code(ComplexAttribute):
    """A complex attribute with a multi-valued and a write-only sub-attribute."""

    value: str | None = None
    lines: list[str] | None = None
    key: Annotated[str | None, Mutability.write_only] = None


class Milestone(ComplexAttribute):
    """Entries with a multi-valued, a read-only and a write-only sub-attribute."""

    label: str | None = None
    steps: list[str] | None = None
    reached: Annotated[bool | None, Mutability.read_only] = None
    token: Annotated[str | None, Mutability.write_only] = None


class Archive(ComplexAttribute):
    """Entries with no stored sub-attribute."""

    items: list[str] | None = None


class Project(Resource):
    """A resource type holding each kind of attribute the generator handles."""

    __schema__ = URN(PROJECT)

    title: str | None = None
    budget: float | None = None
    secret: Annotated[str | None, Mutability.write_only] = None
    pin: Annotated[int | None, Mutability.write_only] = None
    score: Annotated[int | None, Mutability.read_only] = None
    code: Code | None = None
    tags: list[str] | None = None
    codes: Annotated[list[str] | None, Mutability.write_only] = None
    milestones: list[Milestone] | None = None
    archives: list[Archive] | None = None
    members: list[ProjectMember] | None = None
    a_description_long_enough_to_go_beyond_what_postgresql_keeps_in_a_name: (
        str | None
    ) = None


class ProjectLink(ComplexAttribute):
    """A link to a project."""

    value: str | None = None
    ref: Reference["Project"] | None = Field(
        None, serialization_alias="$ref", validation_alias="$ref"
    )
    display: str | None = None


class Staffing(Extension):
    """A user extension with the projects of the user, read-only."""

    __schema__ = URN(STAFFING)

    projects: Annotated[list[ProjectLink] | None, Mutability.read_only] = None


def project_provider(project=Project):
    """Return a provider serving users and projects."""
    return ScimProvider(
        models=[User, Staffing, project],
        resource_types=[
            ResourceType.from_resource(User[Staffing]),
            ResourceType.from_resource(project),
        ],
    )


def test_attributes_the_storage_cannot_hold_are_left_out():
    """A read-only attribute, a write-only number or list, and a list in a complex attribute get no column."""
    metadata = MetaData()
    generate_mappings(project_provider(), metadata, store=hashing_secrets)
    projects = metadata.tables["projects"].c
    milestones = metadata.tables["projects_milestones"].c

    assert {"secret", "code_value", "code_key", "title"} <= set(projects.keys())
    assert not {"pin", "score", "code_lines"} & set(projects.keys())
    assert set(milestones.keys()) == {"id", "resource_id", "label", "token"}
    assert isinstance(projects.budget.type, Float)
    assert isinstance(metadata.tables["projects_tags"].c.value.type, String)
    assert "projects_codes" not in metadata.tables
    assert "projects_archives" not in metadata.tables


def test_write_only_sub_attributes_are_left_out_on_request():
    """Without a hash, a write-only sub-attribute gets no column either."""
    metadata = MetaData()
    generate_mappings(project_provider(), metadata, store=without_secrets)

    assert "code_key" not in metadata.tables["projects"].c
    assert "token" not in metadata.tables["projects_milestones"].c


def test_each_write_only_string_gets_its_own_decision():
    """A secret can be hashed while a key of the same resource is not stored."""
    metadata = MetaData()
    generate_mappings(
        project_provider(),
        metadata,
        store=lambda binding: (
            binding.urn != f"{PROJECT}:code.key" and hashing_secrets(binding)
        ),
    )

    projects = metadata.tables["projects"].c
    assert "secret" in projects
    assert "code_key" not in projects


def test_an_inverse_without_the_attribute_it_reverses_is_left_out():
    """Without groups to serve, the groups of a user have nothing to read."""
    metadata = MetaData()

    mappings = generate_mappings(
        ScimProvider(models=[User]), metadata, store=without_secrets
    )

    (core,) = mappings["User"].schemas()
    assert "groups" not in {attribute.name for attribute in core.attributes}


def test_a_provider_without_write_only_strings_needs_no_decision():
    """The groups hold no password, so their tables need no hash function."""
    metadata = MetaData()

    mappings = generate_mappings(ScimProvider(models=[Group]), metadata)

    assert list(mappings) == ["Group"]


def test_a_long_name_is_shortened():
    """PostgreSQL keeps 63 characters, so a longer name ends with a hash of the full one."""
    metadata = MetaData()
    generate_mappings(project_provider(), metadata, store=without_secrets)
    (name,) = [
        column.name
        for column in metadata.tables["projects"].c
        if column.name.startswith("a_description")
    ]

    assert len(name) == 63
    assert name.startswith("a_description_long_enough_to_go_beyond_what_postgresql")


class CollidingProject(Project):
    """A project whose code_value attribute needs the column of code.value."""

    code_value: str | None = None


def test_two_attributes_needing_the_same_column_are_refused():
    """code.value and codeValue both give code_value."""
    with pytest.raises(ValueError, match="needs the name 'code_value'"):
        generate_mappings(
            project_provider(CollidingProject),
            MetaData(),
            store=without_secrets,
        )


def test_an_inverse_reads_the_links_of_another_attribute(
    database_url, storage_factory, user_type
):
    """Projects of a user, declared as the inverse of members of a project, reads the projects holding the user."""
    provider = project_provider()
    metadata = MetaData()
    mappings = generate_mappings(
        provider,
        metadata,
        prefix="generated_",
        store=without_secrets,
        inverses={f"{STAFFING}:projects": f"{PROJECT}:members"},
    )
    create_tables(database_url, metadata)
    storage = storage_factory(mappings=mappings)
    project_type = provider.resource_types[1]
    alice = storage.create(user_type, User[Staffing](user_name="alice"))

    project = storage.create(
        project_type,
        Project(
            title="Apollo",
            tags=["moon", "nasa"],
            members=[{"value": alice.id}],
            a_description_long_enough_to_go_beyond_what_postgresql_keeps_in_a_name="x",
        ),
    )

    assert project.tags == ["moon", "nasa"]
    assert (
        project.a_description_long_enough_to_go_beyond_what_postgresql_keeps_in_a_name
        == "x"
    )
    projects = storage.get(user_type, alice.id)[Staffing].projects
    assert [p.value for p in projects] == [project.id]
