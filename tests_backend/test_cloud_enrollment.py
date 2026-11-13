import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from cloud_orchestrator.enroll_kaggle import enroll


class FakeStore:
    exists = False
    def __init__(self):
        self.data = None
    def has_versions(self, *_):
        return self.exists
    def add_initial(self, _project, _secret, data):
        self.data = data
        return "projects/fixture/secrets/kcp-kaggle-pilot/versions/1"


def fake_login():
    return "fixture", dict(kind="kaggle_oauth_v1", username="fixture",
                           access_token="FAKE_ACCESS_SENTINEL", refresh_token="FAKE_REFRESH_SENTINEL")


class EnrollmentTests(unittest.TestCase):
    def test_only_metadata_returned_and_no_sensitive_output(self):
        store = FakeStore()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = enroll("research-example", "fixture", "kcp-kaggle-pilot", store, fake_login)
        self.assertEqual(result["status"], "ENROLLED")
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("FAKE_ACCESS_SENTINEL", str(result))
        self.assertNotIn("FAKE_REFRESH_SENTINEL", str(result))
        self.assertIn(b"FAKE_REFRESH_SENTINEL", store.data)

    def test_wrong_account_never_stored(self):
        store = FakeStore()
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            enroll("research-example", "different", "kcp-kaggle-pilot", store, fake_login)
        self.assertIsNone(store.data)

    def test_existing_version_never_read_or_replaced(self):
        store = FakeStore()
        store.exists = True
        with self.assertRaises(ValueError):
            enroll("research-example", "fixture", "kcp-kaggle-pilot", store,
                   lambda: self.fail("login must not run for an existing secret"))
        self.assertIsNone(store.data)

    def test_invalid_target_never_calls_login(self):
        store = FakeStore()
        with self.assertRaises(ValueError):
            enroll("bad/project", "fixture", "kcp-kaggle-pilot", store,
                   lambda: self.fail("invalid destination"))


if __name__ == "__main__":
    unittest.main()
