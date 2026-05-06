#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    test_session_stubs.py

    Regression tests for the local fork's "Stub api.unmanic.app
    dependencies for self-hosted operation" patch. Verify that no Session
    method makes outbound HTTP calls and that level pinning works.
"""
import os
from unittest import mock

import pytest

from unmanic.libs.session import Session, LOCAL_SESSION_LEVEL


def _bare_session():
    """Build a Session without running __init__ (which constructs a
    real requests.Session and a real logger)."""
    s = Session.__new__(Session)
    s.logger = mock.Mock()
    s.uuid = "test-uuid"
    s.level = 0
    s.last_check = None
    s.created = None
    s.user_access_token = None
    s.application_token = None
    s.name = ""
    s.email = ""
    s.picture_uri = ""
    s.requests_session = mock.Mock()  # Catch any accidental HTTP calls
    return s


class TestLocalSessionLevelConstant:

    def test_default_is_seven(self, monkeypatch):
        # Re-importing the module would pick up env at import time, so
        # just verify the runtime constant matches the documented default.
        # If a user sets UNMANIC_LOCAL_SESSION_LEVEL it overrides; we
        # don't probe that here because the import is one-shot.
        if not os.environ.get("UNMANIC_LOCAL_SESSION_LEVEL"):
            assert LOCAL_SESSION_LEVEL == 7


class TestSessionStubsNeverCallApi:
    """Every stubbed method must complete without touching
    requests_session, api_get, or api_post."""

    def test_verify_token_returns_true_without_http(self):
        s = _bare_session()
        assert s.verify_token() is True
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()

    def test_get_access_token_returns_true_without_http(self):
        s = _bare_session()
        assert s.get_access_token() is True
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()

    def test_fetch_user_data_returns_none_without_http(self):
        s = _bare_session()
        assert s.fetch_user_data() is None
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()

    def test_auth_user_account_returns_true_without_http(self):
        s = _bare_session()
        assert s.auth_user_account() is True
        assert s.auth_user_account(force_checkin=True) is True
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()

    def test_auth_trial_account_returns_true_without_http(self):
        s = _bare_session()
        assert s.auth_trial_account() is True
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()

    def test_init_device_auth_flow_returns_false_without_http(self):
        s = _bare_session()
        assert s.init_device_auth_flow() is False
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()

    def test_poll_for_app_token_returns_none_without_http(self):
        s = _bare_session()
        assert s.poll_for_app_token("device_code", interval=1, expires_in=10) is None
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()

    def test_get_patreon_sponsor_page_returns_false_without_http(self):
        s = _bare_session()
        assert s.get_patreon_sponsor_page() is False
        s.requests_session.get.assert_not_called()

    def test_get_credit_portal_funding_proposals_returns_no_data_without_http(self):
        s = _bare_session()
        result = s.get_credit_portal_funding_proposals()
        assert result == (None, 200)
        s.requests_session.get.assert_not_called()


class TestRegisterUnmanicPinsLevel:

    def test_pins_level_to_local_session_level(self):
        s = _bare_session()
        s.level = 0  # simulate fresh install
        with mock.patch.object(s, "_Session__fetch_installation_data"), \
                mock.patch.object(s, "_Session__store_installation_data"), \
                mock.patch.object(s, "_Session__configure_log_forwarding"), \
                mock.patch.object(s, "_Session__update_created_timestamp"), \
                mock.patch.object(s, "_Session__trigger_plugin_repo_refresh_for_level_change") as trig:
            assert s.register_unmanic() is True
        assert s.level == LOCAL_SESSION_LEVEL
        # Level changed 0 -> LOCAL_SESSION_LEVEL, so the repo refresh must
        # have been triggered.
        trig.assert_called_once()

    def test_no_repo_refresh_when_level_unchanged(self):
        s = _bare_session()
        s.level = LOCAL_SESSION_LEVEL  # Already pinned
        with mock.patch.object(s, "_Session__fetch_installation_data"), \
                mock.patch.object(s, "_Session__store_installation_data"), \
                mock.patch.object(s, "_Session__configure_log_forwarding"), \
                mock.patch.object(s, "_Session__update_created_timestamp"), \
                mock.patch.object(s, "_Session__trigger_plugin_repo_refresh_for_level_change") as trig:
            assert s.register_unmanic() is True
        trig.assert_not_called()

    def test_makes_no_http_calls(self):
        s = _bare_session()
        with mock.patch.object(s, "_Session__fetch_installation_data"), \
                mock.patch.object(s, "_Session__store_installation_data"), \
                mock.patch.object(s, "_Session__configure_log_forwarding"), \
                mock.patch.object(s, "_Session__update_created_timestamp"), \
                mock.patch.object(s, "_Session__trigger_plugin_repo_refresh_for_level_change"):
            s.register_unmanic()
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()


class TestSyncRemoteInstallationsStubbed:

    def test_sync_returns_immediately_without_http(self):
        s = _bare_session()
        # Name-mangled because of double-underscore prefix.
        result = s._Session__sync_remote_installation_addresses()
        assert result is None
        s.requests_session.get.assert_not_called()


class TestLoginUrlsReturnEmpty:
    """Upstream returned URLs into api.unmanic.app's OAuth flows. Local
    fork: empty strings so the frontend renders the buttons as no-ops
    rather than links to dead endpoints."""

    def test_get_patreon_login_url_empty(self):
        s = _bare_session()
        assert s.get_patreon_login_url() == ""

    def test_get_github_login_url_empty(self):
        s = _bare_session()
        assert s.get_github_login_url() == ""

    def test_get_discord_login_url_empty(self):
        s = _bare_session()
        assert s.get_discord_login_url() == ""

    def test_get_sign_out_url_empty(self):
        s = _bare_session()
        assert s.get_sign_out_url() == ""


class TestSiteUrlIsUnresolvable:
    """Defense-in-depth: any future regression that does build a URL via
    set_full_api_url should fail loudly at DNS rather than quietly hit
    api.unmanic.app."""

    def test_get_site_url_returns_invalid_tld(self):
        s = _bare_session()
        s.dev_api = None
        assert ".invalid" in s.get_site_url()
        assert "unmanic.app" not in s.get_site_url()

    def test_dev_api_override_still_works(self):
        s = _bare_session()
        s.dev_api = "http://localhost:9999"
        assert s.get_site_url() == "http://localhost:9999"


class TestSignOutSkipsRemote:

    def test_sign_out_does_not_call_remote(self):
        s = _bare_session()
        with mock.patch.object(s, "_Session__reset_session_installation_data") as reset:
            assert s.sign_out() is True
            assert s.sign_out(remote=True) is True  # remote flag now ignored
        # Remote logout endpoint must not be hit.
        s.requests_session.post.assert_not_called()
        # Local cleanup still happens.
        assert reset.call_count == 2
