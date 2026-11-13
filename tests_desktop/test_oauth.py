from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from desktop_app import oauth
from desktop_app import main as desktop_main
from desktop_app.credential_store import WindowsCredentialStore


class _ClientContext:
    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return False


class _Credentials:
    def __init__(self, *_args, **kwargs):
        self._refresh_token = kwargs.get("refresh_token", "refresh-secret")
        self._access_token = kwargs.get("access_token", "access-secret")
        self._access_token_expiration = kwargs.get(
            "access_token_expiration", datetime(2099, 1, 1, tzinfo=timezone.utc)
        )
        self._username = kwargs.get("username", "member-name")
        self._scopes = kwargs.get("scopes", oauth.OAUTH_SCOPES)

    def introspect(self):
        return self._username

    def get_username(self):
        return self._username

    def get_access_token(self):
        return self._access_token


class _OAuth:
    def __init__(self, client):
        self.client = client

    def _run_oauth_flow(self, scopes, no_launch_browser):
        assert scopes == oauth.OAUTH_SCOPES
        assert no_launch_browser is False
        return _Credentials()


class OAuthTests(unittest.TestCase):
    def test_browser_flow_returns_bundle_without_using_plaintext_file(self) -> None:
        with patch.object(oauth, "KaggleClient", _ClientContext), patch.object(
            oauth, "KaggleOAuth", _OAuth
        ):
            username, bundle = oauth.run_browser_oauth()

        self.assertEqual(username, "member-name")
        self.assertEqual(bundle["kind"], oauth.OAUTH_KIND)
        self.assertEqual(bundle["refresh_token"], "refresh-secret")
        self.assertEqual(bundle["access_token"], "access-secret")

    def test_bundle_round_trip_and_access_token_resolution(self) -> None:
        raw = oauth.serialize_oauth_bundle(
            {
                "kind": oauth.OAUTH_KIND,
                "refresh_token": "refresh-secret",
                "access_token": "access-secret",
                "access_token_expiration": "2099-01-01T00:00:00+00:00",
                "username": "member-name",
                "scopes": oauth.OAUTH_SCOPES,
            }
        )
        bundle = oauth.parse_oauth_bundle(raw)
        self.assertIsNotNone(bundle)
        with patch.object(oauth, "KaggleClient", _ClientContext), patch.object(
            oauth, "InMemoryKaggleCredentials", _Credentials
        ):
            token, refreshed = oauth.resolve_oauth_access_token(bundle or {})

        self.assertEqual(token, "access-secret")
        self.assertEqual(refreshed["username"], "member-name")
        self.assertNotIn(" ", json.dumps(refreshed, separators=(",", ":")))

    def test_plain_api_token_is_not_misclassified_as_oauth(self) -> None:
        self.assertIsNone(oauth.parse_oauth_bundle("ordinary-api-token"))
        self.assertIsNone(oauth.parse_oauth_bundle('{"token":"legacy"}'))

    def test_desktop_oauth_flow_saves_only_dpapi_bundle_and_sets_access_token(self) -> None:
        bundle = {
            "kind": oauth.OAUTH_KIND,
            "refresh_token": "refresh-secret",
            "access_token": "access-secret",
            "access_token_expiration": "2099-01-01T00:00:00+00:00",
            "username": "member-name",
            "scopes": oauth.OAUTH_SCOPES,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = desktop_main.Path(temporary)
            store = WindowsCredentialStore(root / "credentials")
            with patch.object(desktop_main, "app_data_root", return_value=root), patch.object(
                desktop_main, "WindowsCredentialStore", return_value=store
            ), patch.object(
                desktop_main, "migrate_legacy_database", return_value=None
            ), patch.object(
                desktop_main, "run_browser_oauth", return_value=("member-name", bundle)
            ):
                runtime = desktop_main.DesktopRuntime(api_port=18765, dashboard_port=13100)
                started = runtime.start_kaggle_oauth("KCP_KAGGLE_MEMBER_99")
                self.assertTrue(started["ok"])
                for _ in range(100):
                    status = runtime.get_kaggle_oauth_status()
                    if status.get("state") != "pending":
                        break
                    time.sleep(0.01)

            self.assertEqual(status["state"], "succeeded")
            self.assertEqual(os.environ["KCP_KAGGLE_MEMBER_99"], "access-secret")
            encrypted = store.path_for("KCP_KAGGLE_MEMBER_99").read_text(encoding="ascii")
            self.assertNotIn("refresh-secret", encrypted)
            stored = oauth.parse_oauth_bundle(store.load("KCP_KAGGLE_MEMBER_99"))
            self.assertEqual(stored and stored["username"], "member-name")
            os.environ.pop("KCP_KAGGLE_MEMBER_99", None)


if __name__ == "__main__":
    unittest.main()
