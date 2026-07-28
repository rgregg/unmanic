#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_plugins_repo_fetch.py

    Tests for the public-catalog direct-fetch path in PluginsHandler:
    - _resolve_direct_repo_url maps "default" and http(s) URLs correctly.
    - _fetch_repo_data_directly returns parsed JSON or None on failure.
    - fetch_remote_repo_data prefers direct fetch and falls back to the
      api.unmanic.app proxy when direct fetch fails.
"""
import json
import logging
import stat
from unittest import mock

import pytest
import requests

from unmanic.libs.plugins import PluginsHandler


def _bare_handler():
    """Construct a PluginsHandler without running __init__ (which would
    instantiate config.Config and the real logger). The methods under test
    only need a logger attribute."""
    h = PluginsHandler.__new__(PluginsHandler)
    h.logger = logging.getLogger("test")
    return h


class TestResolveDirectRepoUrl:

    def test_default_maps_to_official_public_catalog(self, monkeypatch):
        monkeypatch.delenv('UNMANIC_DEFAULT_PLUGIN_REPO_URL', raising=False)
        url = PluginsHandler._resolve_direct_repo_url('default')
        assert url == ('https://raw.githubusercontent.com/'
                       'Unmanic/unmanic-plugins/repo/repo.json')

    def test_default_respects_env_override(self, monkeypatch):
        monkeypatch.setenv('UNMANIC_DEFAULT_PLUGIN_REPO_URL',
                           'https://my-mirror.example.com/repo.json')
        url = PluginsHandler._resolve_direct_repo_url('default')
        assert url == 'https://my-mirror.example.com/repo.json'

    def test_https_url_returned_as_is(self):
        u = 'https://raw.githubusercontent.com/x/y/repo/repo.json'
        assert PluginsHandler._resolve_direct_repo_url(u) == u

    def test_http_url_returned_as_is(self):
        u = 'http://internal-mirror.lan/repo.json'
        assert PluginsHandler._resolve_direct_repo_url(u) == u

    def test_unknown_shortname_returns_none(self):
        # Anything that is not "default" and not a URL falls back to proxy.
        assert PluginsHandler._resolve_direct_repo_url('some-shortname') is None

    def test_non_string_returns_none(self):
        assert PluginsHandler._resolve_direct_repo_url(None) is None
        assert PluginsHandler._resolve_direct_repo_url(123) is None


class TestFetchRepoDataDirectly:

    def test_returns_parsed_json_on_200(self):
        h = _bare_handler()
        body = {'repo': {'name': 'Official'}, 'plugins': []}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = body
        with mock.patch('unmanic.libs.plugins.requests.get', return_value=resp) as g:
            result = h._fetch_repo_data_directly('https://example.com/repo.json')
        assert result == body
        g.assert_called_once_with(
            'https://example.com/repo.json', timeout=10, allow_redirects=True)

    def test_returns_none_on_non_200(self):
        h = _bare_handler()
        resp = mock.Mock(status_code=404)
        with mock.patch('unmanic.libs.plugins.requests.get', return_value=resp):
            assert h._fetch_repo_data_directly('https://example.com/repo.json') is None

    def test_returns_none_on_request_exception(self):
        h = _bare_handler()
        with mock.patch('unmanic.libs.plugins.requests.get',
                        side_effect=requests.exceptions.ConnectTimeout("boom")):
            assert h._fetch_repo_data_directly('https://example.com/repo.json') is None

    def test_returns_none_on_invalid_json(self):
        h = _bare_handler()
        resp = mock.Mock(status_code=200)
        resp.json.side_effect = ValueError("not json")
        with mock.patch('unmanic.libs.plugins.requests.get', return_value=resp):
            assert h._fetch_repo_data_directly('https://example.com/repo.json') is None


class TestFetchRemoteRepoDataRouting:

    def test_url_repo_uses_direct_fetch_and_skips_proxy(self):
        h = _bare_handler()
        body = {'repo': {'name': 'r'}, 'plugins': []}
        with mock.patch.object(h, '_fetch_repo_data_directly',
                               return_value=body) as direct, \
                mock.patch('unmanic.libs.plugins.Session') as SessionCls:
            url = 'https://raw.githubusercontent.com/x/y/repo/repo.json'
            result = h.fetch_remote_repo_data(url)
        assert result == body
        direct.assert_called_once_with(url)
        # Proxy must not be touched at all when direct fetch succeeds.
        SessionCls.assert_not_called()

    def test_default_uses_direct_fetch(self, monkeypatch):
        monkeypatch.delenv('UNMANIC_DEFAULT_PLUGIN_REPO_URL', raising=False)
        h = _bare_handler()
        body = {'repo': {'name': 'Official'}, 'plugins': []}
        with mock.patch.object(h, '_fetch_repo_data_directly',
                               return_value=body) as direct, \
                mock.patch('unmanic.libs.plugins.Session') as SessionCls:
            result = h.fetch_remote_repo_data('default')
        assert result == body
        direct.assert_called_once_with(
            'https://raw.githubusercontent.com/Unmanic/unmanic-plugins/repo/repo.json')
        SessionCls.assert_not_called()

    def test_direct_failure_falls_back_to_proxy(self):
        h = _bare_handler()
        proxy_body = {'repo': {'name': 'via-proxy'}, 'plugins': []}
        sess = mock.Mock()
        sess.get_installation_uuid.return_value = 'uuid-x'
        sess.get_supporter_level.return_value = 0
        sess.api_get.return_value = (proxy_body, 200)
        with mock.patch.object(h, '_fetch_repo_data_directly',
                               return_value=None), \
                mock.patch('unmanic.libs.plugins.Session', return_value=sess):
            result = h.fetch_remote_repo_data(
                'https://raw.githubusercontent.com/x/y/repo/repo.json')
        assert result == proxy_body
        sess.api_get.assert_called_once()

    def test_non_url_shortname_goes_straight_to_proxy(self):
        h = _bare_handler()
        proxy_body = {'repo': {'name': 'via-proxy'}, 'plugins': []}
        sess = mock.Mock()
        sess.get_installation_uuid.return_value = 'uuid-x'
        sess.get_supporter_level.return_value = 0
        sess.api_get.return_value = (proxy_body, 200)
        with mock.patch.object(h, '_fetch_repo_data_directly') as direct, \
                mock.patch('unmanic.libs.plugins.Session', return_value=sess):
            result = h.fetch_remote_repo_data('some-shortname')
        assert result == proxy_body
        # No direct attempt was even made for a non-URL, non-default path.
        direct.assert_not_called()
        sess.api_get.assert_called_once()

    def test_proxy_401_triggers_register_and_retry(self):
        h = _bare_handler()
        proxy_body = {'repo': {'name': 'via-proxy'}, 'plugins': []}
        sess = mock.Mock()
        sess.get_installation_uuid.return_value = 'uuid-x'
        sess.get_supporter_level.return_value = 0
        sess.api_get.side_effect = [(None, 401), (proxy_body, 200)]
        with mock.patch.object(h, '_fetch_repo_data_directly', return_value=None), \
                mock.patch('unmanic.libs.plugins.Session', return_value=sess):
            result = h.fetch_remote_repo_data(
                'https://raw.githubusercontent.com/x/y/repo/repo.json')
        assert result == proxy_body
        assert sess.api_get.call_count == 2
        sess.register_unmanic.assert_called_once()


class TestUpdatePluginRepos:

    @staticmethod
    def _handler_for_cache(cache_path, repo_data):
        h = _bare_handler()
        h.settings = mock.Mock()
        h.settings.get_plugins_path.return_value = str(cache_path.parent)
        h.get_plugin_repos = mock.Mock(return_value=[{'path': 'default'}])
        h.fetch_remote_repo_data = mock.Mock(return_value=repo_data)
        h.get_repo_cache_file = mock.Mock(return_value=str(cache_path))
        return h

    def test_success_atomically_replaces_cache(self, tmp_path):
        cache_path = tmp_path / 'repo.json'
        cache_path.write_text('{"old": true}')
        repo_data = {'repo': {'name': 'Current'}, 'plugins': []}
        h = self._handler_for_cache(cache_path, repo_data)

        assert h.update_plugin_repos() is True
        assert json.loads(cache_path.read_text()) == repo_data
        assert list(tmp_path.glob('.repo.json.*.tmp')) == []

    @pytest.mark.parametrize('repo_data', [None, [], {}, {'repo': {}, 'plugins': {}}])
    def test_invalid_fetch_preserves_existing_cache(self, tmp_path, repo_data):
        cache_path = tmp_path / 'repo.json'
        original = '{"repo": {"name": "Cached"}, "plugins": []}'
        cache_path.write_text(original)
        h = self._handler_for_cache(cache_path, repo_data)

        assert h.update_plugin_repos() is False
        assert cache_path.read_text() == original
        assert list(tmp_path.glob('.repo.json.*.tmp')) == []

    def test_fetch_exception_marks_failure_and_continues(self, tmp_path):
        cache_path = tmp_path / 'repo.json'
        repo_data = {'repo': {'name': 'Second'}, 'plugins': []}
        h = self._handler_for_cache(cache_path, repo_data)
        h.get_plugin_repos.return_value = [
            {'path': 'broken'},
            {'path': 'working'},
        ]
        h.fetch_remote_repo_data.side_effect = [RuntimeError('fetch failed'), repo_data]

        assert h.update_plugin_repos() is False
        assert h.fetch_remote_repo_data.call_count == 2
        assert json.loads(cache_path.read_text()) == repo_data

    def test_replace_failure_preserves_cache_and_removes_temporary_file(self, tmp_path):
        cache_path = tmp_path / 'repo.json'
        original = '{"repo": {"name": "Cached"}, "plugins": []}'
        cache_path.write_text(original)
        repo_data = {'repo': {'name': 'Current'}, 'plugins': []}
        h = self._handler_for_cache(cache_path, repo_data)

        with mock.patch('unmanic.libs.plugins.os.replace',
                        side_effect=OSError('write failed')):
            assert h.update_plugin_repos() is False

        assert cache_path.read_text() == original
        assert list(tmp_path.glob('.repo.json.*.tmp')) == []

    def test_existing_cache_permissions_are_preserved(self, tmp_path):
        cache_path = tmp_path / 'repo.json'
        cache_path.write_text('{"old": true}')
        cache_path.chmod(0o640)
        repo_data = {'repo': {'name': 'Current'}, 'plugins': []}
        h = self._handler_for_cache(cache_path, repo_data)

        assert h.update_plugin_repos() is True
        assert stat.S_IMODE(cache_path.stat().st_mode) == 0o640

    def test_new_cache_permissions_follow_cache_directory(self, tmp_path):
        tmp_path.chmod(0o750)
        cache_path = tmp_path / 'repo.json'
        repo_data = {'repo': {'name': 'Current'}, 'plugins': []}
        h = self._handler_for_cache(cache_path, repo_data)

        assert h.update_plugin_repos() is True
        assert stat.S_IMODE(cache_path.stat().st_mode) == 0o640
