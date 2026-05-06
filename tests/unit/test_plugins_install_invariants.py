#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    test_plugins_install_invariants.py

    Pin the invariants in the plugin install pipeline that the local
    fork's patches depend on. These complement the existing
    test_plugins_zip_safety.py (which tests _assert_zip_members_safe in
    isolation) and test_plugins_repo_fetch.py (which tests catalog
    fetching) by covering the *integration points* — the call ordering
    and side-effect behaviour that a rebase could silently break.

    Invariants covered:

    1. install_plugin calls _assert_zip_members_safe BEFORE extractall.
       Order matters: validation after extraction is no protection.
    2. _assert_zip_members_safe rejection prevents extractall from
       ever running.
    3. download_plugin sends a GET to the plugin's package_url with no
       extra auth headers (the fork relies on direct GitHub URLs that
       don't need auth).
    4. download_plugin times out — never blocks indefinitely on a
       hung server.
    5. notify_site_of_plugin_install is a pure no-op — never reads
       session, never makes HTTP calls.
"""
import json
import logging
import zipfile
from unittest import mock

import pytest

from unmanic.libs.plugins import PluginsHandler


def _bare_handler():
    h = PluginsHandler.__new__(PluginsHandler)
    h.logger = logging.getLogger("test_plugins")
    h.version = 2
    h.settings = mock.Mock()
    return h


def _make_minimal_zip(tmp_path, plugin_id="testplug"):
    """A minimally-valid plugin zip: just info.json with the right id."""
    zpath = tmp_path / "plugin.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("info.json", json.dumps({"id": plugin_id, "version": "1.0"}))
    return str(zpath)


class TestInstallPluginCallsAssertBeforeExtract:

    def test_assert_runs_before_extractall(self, tmp_path):
        """A rebase that adds extractall but skips _assert_zip_members_safe
        would re-introduce the zip-slip vulnerability. Verify the order."""
        h = _bare_handler()
        zip_path = _make_minimal_zip(tmp_path)
        plugin_dir = str(tmp_path / "plugin_install_dest")

        call_order = []

        with mock.patch.object(h, "get_plugin_path", return_value=plugin_dir), \
                mock.patch.object(h, "_assert_zip_members_safe",
                                  side_effect=lambda zr, dd: call_order.append("assert")), \
                mock.patch("unmanic.libs.plugins.zipfile.ZipFile") as MockZip, \
                mock.patch.object(h, "get_plugin_info",
                                  return_value={"id": "testplug"}), \
                mock.patch.object(h, "install_plugin_requirements"):
            zip_ref = MockZip.return_value.__enter__.return_value
            zip_ref.read.return_value = json.dumps(
                {"id": "testplug", "version": "1.0"}).encode()
            zip_ref.extractall.side_effect = lambda dest: call_order.append("extract")

            h.install_plugin(zip_path)

        # _assert_zip_members_safe must run before extractall.
        assert "assert" in call_order, "assert was never called"
        assert "extract" in call_order, "extract was never called"
        assert call_order.index("assert") < call_order.index("extract"), (
            f"assert must come before extract; got order: {call_order}")

    def test_assert_rejection_prevents_extract(self, tmp_path):
        """If _assert_zip_members_safe raises, extractall must never run."""
        h = _bare_handler()
        zip_path = _make_minimal_zip(tmp_path)
        plugin_dir = str(tmp_path / "plugin_install_dest")

        with mock.patch.object(h, "get_plugin_path", return_value=plugin_dir), \
                mock.patch.object(h, "_assert_zip_members_safe",
                                  side_effect=Exception("zip-slip detected")), \
                mock.patch("unmanic.libs.plugins.zipfile.ZipFile") as MockZip:
            zip_ref = MockZip.return_value.__enter__.return_value
            zip_ref.read.return_value = json.dumps(
                {"id": "testplug", "version": "1.0"}).encode()

            with pytest.raises(Exception, match="zip-slip"):
                h.install_plugin(zip_path)

            zip_ref.extractall.assert_not_called()


class TestDownloadPluginUsesPackageUrlDirectly:
    """The fork's direct-fetch path puts the real GitHub URL in
    package_url. download_plugin must GET it without injecting any
    auth header that would confuse a public CDN."""

    def test_get_called_with_package_url_no_extra_headers(self, tmp_path):
        h = _bare_handler()
        download_dest = str(tmp_path / "p-1.0.zip")

        # A fake response that yields a single chunk and 200 status.
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [b"hello"]
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)

        fake_session = mock.Mock()
        fake_session.requests_session.get.return_value = resp
        fake_session.timeout = 10

        plugin = {
            "plugin_id": "p", "version": "1.0",
            "package_url": "https://raw.githubusercontent.com/x/y/repo/p/p-1.0.zip",
        }

        with mock.patch.object(h, "get_plugin_download_cache_path",
                               return_value=download_dest), \
                mock.patch("unmanic.libs.plugins.Session", return_value=fake_session):
            result = h.download_plugin(plugin)

        assert result == download_dest
        # Exactly one GET, to the package_url, with no auth headers added.
        fake_session.requests_session.get.assert_called_once()
        args, kwargs = fake_session.requests_session.get.call_args
        assert args[0] == "https://raw.githubusercontent.com/x/y/repo/p/p-1.0.zip"
        # The fork relies on these public URLs not needing auth — verify
        # we didn't smuggle any headers in.
        assert "headers" not in kwargs or not kwargs["headers"]

    def test_get_uses_session_timeout(self, tmp_path):
        """Never let a hung server block indefinitely."""
        h = _bare_handler()
        download_dest = str(tmp_path / "p-1.0.zip")

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [b""]
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)

        fake_session = mock.Mock()
        fake_session.requests_session.get.return_value = resp
        fake_session.timeout = 30

        plugin = {"plugin_id": "p", "version": "1.0",
                  "package_url": "https://example.com/p-1.0.zip"}

        with mock.patch.object(h, "get_plugin_download_cache_path",
                               return_value=download_dest), \
                mock.patch("unmanic.libs.plugins.Session", return_value=fake_session):
            h.download_plugin(plugin)

        kwargs = fake_session.requests_session.get.call_args.kwargs
        assert kwargs.get("timeout") == 30
        # Streaming for large files; allow_redirects so GitHub redirects
        # to S3 work transparently.
        assert kwargs.get("stream") is True
        assert kwargs.get("allow_redirects") is True


class TestNotifyPluginInstallIsPureNoop:
    """Already partially covered in test_phone_home_misc_stubs.py; this
    extends with the "no HTTP, no Session, no exceptions on weird input"
    invariants. The upstream implementation built a payload from
    plugin.get('plugin_id'), .get('author'), .get('version') etc — a
    rebase that drops our stub would re-introduce all of those calls."""

    def test_returns_none_for_valid_plugin(self):
        h = _bare_handler()
        with mock.patch("unmanic.libs.plugins.Session") as session_cls, \
                mock.patch("unmanic.libs.plugins.requests") as req:
            assert h.notify_site_of_plugin_install(
                {"plugin_id": "p", "author": "a", "version": "1.0"}) is None
        session_cls.assert_not_called()
        req.post.assert_not_called()
        req.get.assert_not_called()

    def test_returns_none_for_missing_fields(self):
        """Upstream would have crashed (or sent {None: None} JSON) with
        an empty dict. Stub must be defensive."""
        h = _bare_handler()
        with mock.patch("unmanic.libs.plugins.Session") as session_cls, \
                mock.patch("unmanic.libs.plugins.requests") as req:
            assert h.notify_site_of_plugin_install({}) is None
            assert h.notify_site_of_plugin_install({"plugin_id": "x"}) is None
        session_cls.assert_not_called()
        req.post.assert_not_called()
