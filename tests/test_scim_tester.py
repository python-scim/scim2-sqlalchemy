"""Check a SCIM server built on the storage with scim2-tester.

https://github.com/python-scim/scim2-tester
"""

import pytest
from scim2_client.engines.wsgi import WSGISCIMClient
from scim2_server.applications.wsgi import WSGIApplication
from scim2_tester import Status
from scim2_tester import check_server
from werkzeug.test import Client

GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"


@pytest.fixture
def scim_client(sync_storage_factory, restricted_provider):
    storage = sync_storage_factory(provider=restricted_provider)
    application = WSGIApplication(storage, restricted_provider)
    return WSGISCIMClient(
        application, base_url="http://localhost/v2", provider=restricted_provider
    )


def test_the_server_passes_scim2_tester(scim_client):
    """A server publishing the schemas of the mappings passes every check it announces."""
    results = check_server(scim_client)

    failures = [
        (result.title, result.reason)
        for result in results
        if result.status not in (Status.SKIPPED, Status.SUCCESS)
    ]
    assert failures == []


def test_a_patch_adding_a_member_without_value_is_refused(
    sync_storage_factory, restricted_provider, user_type
):
    """A PATCH adding a member with a $ref only answers 400, and the group keeps no member."""
    storage = sync_storage_factory(provider=restricted_provider)
    application = WSGIApplication(storage, restricted_provider)
    client = Client(application)
    group = client.post(
        "/v2/Groups",
        json={"schemas": [GROUP_SCHEMA], "displayName": "admins"},
    ).json
    patch = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {
                "op": "add",
                "path": "members",
                "value": [{"$ref": "https://example.org/v2/Users/1"}],
            }
        ],
    }

    response = client.patch(f"/v2/Groups/{group['id']}", json=patch)

    assert response.status_code == 400
    assert response.json["scimType"] == "invalidValue"
    assert "members" not in client.get(f"/v2/Groups/{group['id']}").json
