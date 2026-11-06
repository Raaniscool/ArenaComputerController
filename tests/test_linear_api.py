import base64
import contextlib
import io
import json
import tempfile
import unittest
import urllib.parse
import urllib.error
from pathlib import Path

from arena_workshop.linear_api import (
    GRAPHQL_URL,
    OAUTH_SCOPES,
    TOKEN_URL,
    LinearApi,
    LinearApiError,
    LinearCredentialStore,
)
from arena_workshop.linear_cli import configure_credentials, submit_main
from arena_workshop.protocol import JobValidationError, parse_job_description, validate_job_description


class FakeResponse:
    def __init__(self, value, status=200):
        self.body = json.dumps(value).encode("utf-8")
        self.status = status

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class LinearApiTests(unittest.TestCase):
    def test_client_credentials_request_uses_fixed_least_scopes(self):
        requests = []

        def opener(request, timeout=30):
            requests.append(request)
            return FakeResponse({"access_token": "test-access-token", "expires_in": 100})

        api = LinearApi("public-client-id", "private-client-secret", opener=opener)
        api.authenticate()
        self.assertEqual(api._access_token, "test-access-token")
        request = requests[0]
        self.assertEqual(request.full_url, TOKEN_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        expected_auth = "Basic " + base64.b64encode(b"public-client-id:private-client-secret").decode("ascii")
        self.assertEqual(request.get_header("Authorization"), expected_auth)
        form = urllib.parse.parse_qs(request.data.decode("ascii"))
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["scope"], [",".join(OAUTH_SCOPES)])
        self.assertEqual(set(OAUTH_SCOPES), {"read", "issues:create", "comments:create"})

    def test_job_team_and_label_are_resolved_by_exact_key_and_name(self):
        requests = []

        def opener(request, timeout=30):
            body = json.loads(request.data.decode("utf-8"))
            requests.append(body)
            if "ArenaBlenderTeam" in body["query"]:
                return FakeResponse({"data": {"teams": {"nodes": [
                    {"id": "team-id", "key": "BLE"},
                ]}}})
            if "ArenaBlenderIssueLabel" in body["query"]:
                return FakeResponse({"data": {"issueLabels": {"nodes": [
                    {"id": "label-id", "name": "arena-blender-job", "isGroup": False},
                ]}}})
            raise AssertionError("Unexpected GraphQL request")

        api = LinearApi("client", "secret", opener=opener)
        api._access_token = "test-token"
        team_id = api.get_team_id("BLE")
        label_id = api.get_issue_label_id(team_id, "arena-blender-job")

        self.assertEqual(team_id, "team-id")
        self.assertEqual(label_id, "label-id")
        self.assertEqual(requests[0]["variables"], {"teamKey": "BLE"})
        self.assertEqual(requests[1]["variables"], {"teamId": "team-id", "labelName": "arena-blender-job"})

    def test_graphql_401_fetches_one_new_in_memory_token_and_retries(self):
        requests = []
        token_count = 0
        graphql_count = 0

        def opener(request, timeout=30):
            nonlocal token_count, graphql_count
            requests.append(request)
            if request.full_url == TOKEN_URL:
                token_count += 1
                return FakeResponse({"access_token": "token-{}".format(token_count)})
            graphql_count += 1
            if graphql_count == 1:
                raise urllib.error.HTTPError(GRAPHQL_URL, 401, "Unauthorized", {}, None)
            return FakeResponse({"data": {"viewer": {"id": "worker"}}})

        api = LinearApi("client", "secret", opener=opener)
        data = api.graphql("query { viewer { id } }")
        self.assertEqual(data["viewer"]["id"], "worker")
        self.assertEqual(token_count, 2)
        graph_requests = [request for request in requests if request.full_url == GRAPHQL_URL]
        self.assertEqual(graph_requests[0].get_header("Authorization"), "Bearer token-1")
        self.assertEqual(graph_requests[1].get_header("Authorization"), "Bearer token-2")
        self.assertEqual(api._access_token, "token-2")

    def test_authentication_failure_is_safe_and_does_not_leak_secret(self):
        secret = "do-not-print-this"

        def opener(request, timeout=30):
            body = json.dumps({"error": "invalid_client", "error_description": "Rejected " + secret}).encode("utf-8")
            raise urllib.error.HTTPError(TOKEN_URL, 401, "Unauthorized", {}, io.BytesIO(body))

        api = LinearApi("client-id", secret, opener=opener)
        with self.assertRaises(LinearApiError) as raised:
            api.authenticate()
        self.assertEqual(raised.exception.code, "unauthorized")
        self.assertEqual(raised.exception.endpoint, "OAuth token endpoint")
        self.assertNotIn(secret, str(raised.exception))
        self.assertIsNone(api._access_token)

    def test_graphql_errors_are_not_reported_as_success(self):
        def opener(request, timeout=30):
            if request.full_url == TOKEN_URL:
                return FakeResponse({"access_token": "access"})
            return FakeResponse({"data": {}, "errors": [{
                "message": "Permission denied for Bearer access",
                "extensions": {"code": "FORBIDDEN"},
            }]})

        api = LinearApi("client", "secret", opener=opener)
        with self.assertRaises(LinearApiError) as raised:
            api.graphql("query { viewer { id } }")
        self.assertEqual(raised.exception.code, "graphql_error")
        self.assertEqual(raised.exception.status, 200)
        self.assertEqual(raised.exception.endpoint, "GraphQL endpoint")
        self.assertIn("FORBIDDEN", str(raised.exception))
        self.assertIn("Permission denied", str(raised.exception))
        self.assertNotIn("Bearer access", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_http_error_reports_status_and_safe_linear_details_without_tokens(self):
        token = "sensitive-access-token"
        detail = "OAuth token " + token + " was denied by policy"

        def opener(request, timeout=30):
            if request.full_url == TOKEN_URL:
                return FakeResponse({"access_token": token})
            body = json.dumps({"errors": [{
                "message": detail,
                "extensions": {"code": "FORBIDDEN"},
                "debug": "must not be printed",
            }]}).encode("utf-8")
            raise urllib.error.HTTPError(GRAPHQL_URL, 403, "Forbidden", {}, io.BytesIO(body))

        api = LinearApi("client", "secret", opener=opener)
        with self.assertRaises(LinearApiError) as raised:
            api.graphql("query { viewer { id } }")
        self.assertEqual(raised.exception.code, "linear_http_error")
        self.assertEqual(raised.exception.status, 403)
        self.assertIn("FORBIDDEN", raised.exception.message)
        self.assertIn("was denied by policy", raised.exception.message)
        self.assertNotIn(token, raised.exception.message)
        self.assertNotIn("must not be printed", raised.exception.message)

    def test_unstructured_http_error_never_echoes_response_body(self):
        secret = "do-not-leak"

        def opener(request, timeout=30):
            body = b"<html>Bearer " + secret.encode("ascii") + b" internal stack trace</html>"
            raise urllib.error.HTTPError(TOKEN_URL, 400, "Bad Request", {}, io.BytesIO(body))

        api = LinearApi("client", secret, opener=opener)
        with self.assertRaises(LinearApiError) as raised:
            api.authenticate()
        self.assertEqual(raised.exception.status, 400)
        self.assertNotIn(secret, raised.exception.message)
        self.assertNotIn("internal stack trace", raised.exception.message)

    def test_job_description_validation_accepts_nested_objects_arrays_and_escaped_strings(self):
        request = {
            "operation": "create_primitive",
            "parameters": {
                "primitive_type": "cube",
                "object_name": 'Quoted "Village\\Gate',
                "location": [1.25, -2, 3],
                "rotation": [0, 45, 0],
                "scale": [1, 2, 0.5],
            },
        }
        description = json.dumps(request, separators=(",", ":"))
        self.assertIn('\\"', description)
        self.assertIn('\\\\', description)
        request = validate_job_description(description)
        self.assertEqual(request["parameters"]["location"], [1.25, -2, 3])
        self.assertEqual(request["parameters"]["scale"], [1, 2, 0.5])
        self.assertEqual(request["parameters"]["object_name"], 'Quoted "Village\\Gate')
        self.assertEqual(parse_job_description(description), request)

        string_request = {
            "operation": "hello_test",
            "parameters": {"message": 'He said "Welcome\\home"'},
        }
        string_description = json.dumps(string_request, separators=(",", ":"))
        self.assertEqual(validate_job_description(string_description), string_request)

    def test_issue_creation_rejects_missing_or_malformed_descriptions_before_network(self):
        calls = []
        api = LinearApi("client", "secret", opener=lambda *args, **kwargs: calls.append(args))
        api._access_token = "test-token"
        invalid = (
            None,
            "",
            "not json",
            '{"operation":"hello_test","parameters":{}}',
            '{"operation":"create_primitive","operation":"delete_object","parameters":{}}',
        )
        for description in invalid:
            with self.subTest(description=description), self.assertRaises(LinearApiError) as raised:
                api.create_issue("team", "title", description, "backlog", ["label"])
            self.assertEqual(raised.exception.code, "invalid_job_description")
        self.assertEqual(calls, [])

    def test_create_and_get_issue_round_trip_preserves_raw_json_and_structure(self):
        expected = {
            "operation": "create_primitive",
            "parameters": {
                "primitive_type": "cube",
                "object_name": 'Quoted "Village\\Gate',
                "location": [1.25, -2, 3],
                "rotation": [0, 45, 0],
                "scale": [1, 2, 0.5],
            },
        }
        description = json.dumps(expected, separators=(",", ":"))
        self.assertEqual(validate_job_description(description), expected)
        requests = []
        stored = {}

        def opener(request, timeout=30):
            body = json.loads(request.data.decode("utf-8"))
            requests.append(body)
            if "issueCreate" in body["query"]:
                variables = body["variables"]
                stored.update({
                    "id": "issue-uuid-BLE-99",
                    "identifier": "BLE-99",
                    "title": variables["title"],
                    "description": variables["description"],
                    "team": {"key": "BLE"},
                    "state": {"name": "Backlog"},
                    "labels": {"nodes": [{"name": "arena-blender-job"}]},
                })
                return FakeResponse({"data": {"issueCreate": {"success": True, "issue": dict(stored)}}})
            if "ArenaBlenderIssue" in body["query"]:
                return FakeResponse({"data": {"issue": dict(stored)}})
            raise AssertionError("Unexpected GraphQL request")

        api = LinearApi("client", "secret", opener=opener)
        api._access_token = "test-token"
        created = api.create_issue(
            team_id="team-id", title="JSON round trip", description=description,
            state_id="backlog-id", label_ids=["label-id"],
        )
        retrieved = api.get_issue(created["identifier"])

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["variables"]["description"], description)
        self.assertEqual(created["description"], description)
        self.assertEqual(retrieved["description"], description)
        self.assertEqual(validate_job_description(retrieved["description"]), expected)
        self.assertEqual(json.loads(retrieved["description"]), expected)

    def test_network_outage_is_retryable(self):
        def opener(request, timeout=30):
            raise urllib.error.URLError("offline")

        api = LinearApi("client", "secret", opener=opener)
        with self.assertRaises(LinearApiError) as raised:
            api.authenticate()
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("secret", str(raised.exception))


class LinearSubmitCliTests(unittest.TestCase):
    class Store:
        def load(self):
            return {"client_id": "client", "client_secret": "secret"}

    class Api:
        def __init__(self, client_id, client_secret):
            self.issue = None
            self.state_updates = []

        def get_team_id(self, team_key):
            self.assert_team = team_key
            return "team-id"

        def get_workflow_states(self, team_key):
            return {"Backlog": "backlog-id", "Todo": "todo-id"}

        def get_issue_label_id(self, team_id, label_name):
            self.assert_label = (team_id, label_name)
            return "label-id"

        def create_issue(self, **kwargs):
            self.issue = {
                "id": "issue-id",
                "identifier": "BLE-900",
                "description": kwargs["description"],
                "state": {"name": "Backlog"},
                "team": {"key": "BLE"},
                "labels": {"nodes": [{"name": "arena-blender-job"}]},
            }
            return dict(self.issue)

        def get_issue(self, identifier):
            assert identifier == "BLE-900"
            return dict(self.issue)

        def update_issue_state(self, issue_id, state_id):
            assert issue_id == "issue-id"
            self.state_updates.append(state_id)
            self.issue["state"] = {"name": "Todo"}

    def test_submission_verifies_description_before_optional_dispatch(self):
        expected = {
            "operation": "create_primitive",
            "parameters": {
                "primitive_type": "cube",
                "object_name": 'Gate "North\\West',
                "location": [0, 2, -0.25],
                "scale": [1.5, 1, 0.25],
            },
        }
        description = json.dumps(expected, separators=(",", ":"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_bytes(description.encode("utf-8"))
            api = self.Api("client", "secret")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                status = submit_main(
                    ["--title", "Queued job", "--description-file", str(path), "--dispatch"],
                    store=self.Store(), api_factory=lambda *args: api,
                )

        self.assertEqual(status, 0)
        self.assertEqual(api.issue["description"], description)
        self.assertEqual(api.issue["state"]["name"], "Todo")
        self.assertEqual(api.state_updates, ["todo-id"])
        self.assertTrue(json.loads(output.getvalue())["description_verified"])

    def test_submission_error_shows_phase_and_http_status(self):
        class FailingApi:
            def __init__(self, client_id, client_secret):
                pass

            def get_team_id(self, team_key):
                raise LinearApiError(
                    "linear_http_error", "FORBIDDEN: Team access denied", status=403,
                    endpoint="GraphQL endpoint",
                )

        description = json.dumps({"operation": "hello_test", "parameters": {"message": "smoke"}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_text(description, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as error_output:
                status = submit_main(
                    ["--title", "Diagnostic", "--description-file", str(path)],
                    store=self.Store(), api_factory=FailingApi,
                )

        self.assertEqual(status, 1)
        self.assertIn("during resolve team BLE via GraphQL endpoint", error_output.getvalue())
        self.assertIn("linear_http_error, HTTP 403", error_output.getvalue())
        self.assertIn("FORBIDDEN: Team access denied", error_output.getvalue())

    def test_submission_keeps_unverified_or_invalid_jobs_out_of_todo(self):
        class UnusedStore:
            def load(self):
                raise AssertionError("credentials should not load for invalid descriptions")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{bad", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                status = submit_main(
                    ["--title", "Bad job", "--description-file", str(path)],
                    store=UnusedStore(),
                )
            self.assertEqual(status, 2)
            with contextlib.redirect_stderr(io.StringIO()):
                missing_status = submit_main(
                    ["--title", "Missing job", "--description-file", str(Path(directory) / "missing.json")],
                    store=UnusedStore(),
                )
            self.assertEqual(missing_status, 2)


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "linear_credentials.dpapi"

    def tearDown(self):
        self.temporary.cleanup()

    def test_credentials_are_saved_only_as_injected_protected_bytes(self):
        seen = []

        def protect(raw):
            seen.append(raw)
            return b"protected:" + raw[::-1]

        def unprotect(value):
            self.assertTrue(value.startswith(b"protected:"))
            return value[len(b"protected:"):][::-1]

        store = LinearCredentialStore(self.path, protect=protect, unprotect=unprotect)
        store.save("client-id", "secret-value")
        self.assertNotIn(b"secret-value", self.path.read_bytes())
        self.assertEqual(store.load(), {"client_id": "client-id", "client_secret": "secret-value"})
        self.assertEqual(len(seen), 1)

    def test_no_plaintext_fallback_when_dpapi_fails(self):
        store = LinearCredentialStore(self.path, protect=lambda value: (_ for _ in ()).throw(OSError("DPAPI unavailable")))
        with self.assertRaises(LinearApiError) as raised:
            store.save("client-id", "secret-value")
        self.assertEqual(raised.exception.code, "credential_protection_failed")
        self.assertFalse(self.path.exists())

    def test_setup_validates_then_saves_and_discards_access_token(self):
        saved = {}

        class Store:
            def save(self, client_id, client_secret):
                saved["client_id"] = client_id
                saved["client_secret"] = client_secret

        class Api:
            def __init__(self, client_id, client_secret):
                saved["api_credentials"] = (client_id, client_secret)
                self.forgot = False

            def authenticate_and_forget(self):
                self.forgot = True
                saved["forgot"] = True

        configure_credentials(" client-id ", "secret-value", store=Store(), api_factory=Api)
        self.assertEqual(saved["client_id"], "client-id")
        self.assertEqual(saved["client_secret"], "secret-value")
        self.assertTrue(saved["forgot"])


if __name__ == "__main__":
    unittest.main()
