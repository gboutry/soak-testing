from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from soak_tester.openstack_client.identity import create_project_user


@pytest.mark.parametrize("failure", ["user", "role_assignment"])
def test_identity_creation_records_each_resource_before_the_next_failure(failure):
    identity = Mock()
    identity.find_role.return_value = SimpleNamespace(id="member")
    identity.create_project.return_value = SimpleNamespace(id="project", name="owned")
    identity.create_user.return_value = SimpleNamespace(id="user", name="owned")
    recorded = []

    def fail(*args, **kwargs):
        expected = ["project"] if failure == "user" else ["project", "user"]
        assert [kind for kind, _ in recorded] == expected
        raise RuntimeError("injected partial identity failure")

    if failure == "user":
        identity.create_user.side_effect = fail
    else:
        identity.assign_project_role_to_user.side_effect = fail
    with pytest.raises(RuntimeError, match="partial identity"):
        create_project_user(
            SimpleNamespace(identity=identity),
            project_name="owned",
            username="owned",
            role_name="member",
            record_created=lambda kind, resource: recorded.append((kind, resource)),
        )


def test_missing_member_role_fails_before_creating_identity_resources():
    identity = Mock()
    identity.find_role.return_value = None
    with pytest.raises(RuntimeError, match="role not found"):
        create_project_user(
            SimpleNamespace(identity=identity),
            project_name="owned",
            username="owned",
            role_name="member",
            record_created=Mock(),
        )
    identity.create_project.assert_not_called()
    identity.create_user.assert_not_called()


def test_identity_uses_requested_domains_before_creation():
    identity = Mock()
    identity.find_role.return_value = SimpleNamespace(id="member")
    identity.find_domain.side_effect = [
        SimpleNamespace(id="project-domain"),
        SimpleNamespace(id="user-domain"),
    ]
    identity.create_project.return_value = SimpleNamespace(id="p", name="run")
    identity.create_user.return_value = SimpleNamespace(id="u", name="run")
    create_project_user(
        SimpleNamespace(identity=identity),
        project_name="run",
        username="run",
        role_name="member",
        record_created=Mock(),
        description="owned",
        project_domain_name="projects",
        user_domain_name="users",
    )
    assert identity.create_project.call_args.kwargs["domain_id"] == "project-domain"
    assert identity.create_user.call_args.kwargs["domain_id"] == "user-domain"
