from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from io import BytesIO
from pathlib import Path

from desktop_app.credential_store import WindowsCredentialStore
from desktop_app.main import fetch_update_status, update_available


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class DesktopCredentialStoreTests(unittest.TestCase):
    def test_dpapi_round_trip_and_forget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WindowsCredentialStore(Path(temporary))
            ref = store.save("kcp_test_member_01", "private-test-token")
            self.assertEqual(ref, "KCP_TEST_MEMBER_01")
            self.assertEqual(store.list_refs(), [ref])
            self.assertEqual(store.load(ref), "private-test-token")
            self.assertNotIn("private-test-token", store.path_for(ref).read_text(encoding="ascii"))
            self.assertTrue(store.forget(ref))
            self.assertEqual(store.list_refs(), [])

    def test_rejects_unsafe_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WindowsCredentialStore(Path(temporary))
            with self.assertRaises(ValueError):
                store.save("NOT_A_KCP_REF", "token")


class DesktopUpdateTests(unittest.TestCase):
    def test_semver_update_comparison_handles_prereleases(self) -> None:
        self.assertTrue(update_available("0.2.0-beta.2", "0.2.0-beta.10"))
        self.assertTrue(update_available("0.2.0-beta.10", "0.2.0"))
        self.assertFalse(update_available("0.2.0", "0.2.0-beta.10"))

    @patch("desktop_app.main.build_identity", return_value={"version": "0.2.0-beta.1", "build_sha": "test"})
    @patch("desktop_app.main.urllib.request.urlopen")
    def test_update_check_uses_latest_non_draft_release(self, urlopen, _identity) -> None:
        urlopen.return_value = _Response(
            b'[{"tag_name":"v0.2.0-beta.2","html_url":"https://github.com/TQC0103/kaggle-team-control-plane/releases/tag/v0.2.0-beta.2","draft":false}]'
        )
        result = fetch_update_status()
        self.assertTrue(result["ok"])
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest"], "0.2.0-beta.2")


if __name__ == "__main__":
    unittest.main()
