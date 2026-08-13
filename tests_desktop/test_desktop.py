from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from desktop_app.credential_store import WindowsCredentialStore


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


if __name__ == "__main__":
    unittest.main()
