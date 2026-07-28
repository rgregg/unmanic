import json
from pathlib import Path

import yaml


SCHEMA_DIRECTORY = (
    Path(__file__).resolve().parents[2]
    / "unmanic"
    / "webserver"
    / "docs"
)


def _assert_disabled_session_endpoints(schema):
    expected_descriptions = {
        "/session/logout": (
            "The upstream central account service is not supported by this distribution.",
            "Central account logout is not supported.",
        ),
        "/session/get_app_auth_code": (
            "The upstream central account service is not supported by this distribution.",
            "Central account authentication is not supported.",
        ),
        "/session/funding_proposals": (
            "The upstream central funding service is not supported by this distribution.",
            "Central funding proposals are not supported.",
        ),
    }

    for path, (operation_description, response_description) in expected_descriptions.items():
        operation = schema["paths"][path]["get"]
        assert operation["description"] == operation_description
        responses = operation["responses"]
        assert set(responses) == {"410"}
        assert "200" not in responses
        assert responses["410"]["description"] == response_description
        assert responses["410"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/BaseError"
        }


def test_disabled_session_endpoints_only_advertise_410_base_error():
    json_schema = json.loads(
        (SCHEMA_DIRECTORY / "api_schema_v2.json").read_text(encoding="utf-8")
    )
    yaml_schema = yaml.safe_load(
        (SCHEMA_DIRECTORY / "api_schema_v2.yaml").read_text(encoding="utf-8")
    )

    _assert_disabled_session_endpoints(json_schema)
    _assert_disabled_session_endpoints(yaml_schema)


def test_error_schemas_document_the_common_error_contract():
    json_schema = json.loads(
        (SCHEMA_DIRECTORY / "api_schema_v2.json").read_text(encoding="utf-8")
    )
    yaml_schema = yaml.safe_load(
        (SCHEMA_DIRECTORY / "api_schema_v2.yaml").read_text(encoding="utf-8")
    )

    for schema in (json_schema, yaml_schema):
        for name in (
                "BaseError", "BadRequest", "BadEndpoint", "BadMethod",
                "Conflict", "InternalError"):
            error_schema = schema["components"]["schemas"][name]
            assert set(error_schema["required"]) >= {"error", "error_code", "messages"}


def test_failed_task_recovery_contract_is_documented():
    schemas = [
        json.loads((SCHEMA_DIRECTORY / "api_schema_v2.json").read_text(encoding="utf-8")),
        yaml.safe_load((SCHEMA_DIRECTORY / "api_schema_v2.yaml").read_text(encoding="utf-8")),
    ]

    for schema in schemas:
        assert "post" in schema["paths"]["/history/dismiss"]
        result = schema["components"]["schemas"]["CompletedTasksTableResults"]
        assert set(result["properties"]) >= {
            "failure_category",
            "failure_message",
            "failure_time",
            "dismissed_at",
        }


def test_remote_pre_ready_mutations_document_conflicts():
    schemas = [
        json.loads((SCHEMA_DIRECTORY / "api_schema_v2.json").read_text(
            encoding="utf-8")),
        yaml.safe_load((SCHEMA_DIRECTORY / "api_schema_v2.yaml").read_text(
            encoding="utf-8")),
    ]

    for schema in schemas:
        for path in (
                "/pending/library/update",
                "/pending/status/set/ready"):
            conflict = schema["paths"][path]["post"]["responses"]["409"]
            assert conflict["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/Conflict"}
