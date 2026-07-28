#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_plugins_repo_cache.py

    Tests for PluginsHandler.update_plugin_repos() cache handling:
    - a successful refresh replaces the cache and reports success,
    - a network failure (fetch returns None or raises) preserves the last
      valid cache and reports failure,
    - malformed / non-catalog data never overwrites a good cache,
    - a serialisation failure leaves the previous cache intact (atomic
      replacement, no truncated file), and
    - one failing repo does not stop the others from refreshing.
"""
import json
import logging
import os
from unittest import mock

import pytest

from trawlarr.libs.plugins import PluginsHandler

GOOD_CATALOG = {'repo': {'name': 'Official'}, 'plugins': [{'id': 'x'}]}
STALE_CATALOG = {'repo': {'name': 'Official'}, 'plugins': [{'id': 'stale'}]}


@pytest.fixture
def handler(tmp_path):
    """A PluginsHandler with no __init__ side effects, caching into tmp_path."""
    h = PluginsHandler.__new__(PluginsHandler)
    h.logger = logging.getLogger("test")
    h.settings = mock.Mock()
    h.settings.get_plugins_path.return_value = str(tmp_path)
    return h


def _cache_file(handler, repo_path):
    return handler.get_repo_cache_file(PluginsHandler.get_plugin_repo_id(repo_path))


def _seed_cache(handler, repo_path, data):
    path = _cache_file(handler, repo_path)
    with open(path, 'w') as f:
        json.dump(data, f)
    return path


def _single_repo(handler, repo_path='default'):
    return mock.patch.object(handler, 'get_plugin_repos',
                             return_value=[{'path': repo_path}])


class TestSuccessfulRefresh:

    def test_writes_fetched_catalog_and_returns_true(self, handler):
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data',
                                  return_value=GOOD_CATALOG):
            assert handler.update_plugin_repos() is True
        with open(_cache_file(handler, 'default')) as f:
            assert json.load(f) == GOOD_CATALOG

    def test_replaces_a_previously_cached_catalog(self, handler):
        _seed_cache(handler, 'default', STALE_CATALOG)
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data',
                                  return_value=GOOD_CATALOG):
            assert handler.update_plugin_repos() is True
        with open(_cache_file(handler, 'default')) as f:
            assert json.load(f) == GOOD_CATALOG

    def test_leaves_no_temporary_files_behind(self, handler, tmp_path):
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data',
                                  return_value=GOOD_CATALOG):
            handler.update_plugin_repos()
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith('.tmp')] == []

    def test_preserves_the_mode_of_the_replaced_cache(self, handler):
        cache = _seed_cache(handler, 'default', STALE_CATALOG)
        os.chmod(cache, 0o640)
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data',
                                  return_value=GOOD_CATALOG):
            handler.update_plugin_repos()
        assert os.stat(cache).st_mode & 0o777 == 0o640


class TestNetworkFailure:

    def test_fetch_returning_none_preserves_cache_and_reports_failure(self, handler):
        cache = _seed_cache(handler, 'default', STALE_CATALOG)
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data', return_value=None):
            assert handler.update_plugin_repos() is False
        with open(cache) as f:
            assert json.load(f) == STALE_CATALOG

    def test_fetch_raising_preserves_cache_and_reports_failure(self, handler):
        cache = _seed_cache(handler, 'default', STALE_CATALOG)
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data',
                                  side_effect=ConnectionError("boom")):
            assert handler.update_plugin_repos() is False
        with open(cache) as f:
            assert json.load(f) == STALE_CATALOG

    def test_failure_logs_the_repo_path(self, handler, caplog):
        with _single_repo(handler, 'https://mirror.example.com/repo.json'), \
                mock.patch.object(handler, 'fetch_remote_repo_data', return_value=None):
            with caplog.at_level(logging.ERROR, logger='test'):
                handler.update_plugin_repos()
        assert 'https://mirror.example.com/repo.json' in caplog.text

    def test_no_cache_file_is_created_when_the_fetch_fails(self, handler):
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data', return_value=None):
            assert handler.update_plugin_repos() is False
        assert not os.path.exists(_cache_file(handler, 'default'))


class TestMalformedData:

    @pytest.mark.parametrize('payload', [None, {}, [], 'not a catalog', 0])
    def test_non_catalog_payloads_never_overwrite_a_good_cache(self, handler, payload):
        cache = _seed_cache(handler, 'default', STALE_CATALOG)
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data', return_value=payload):
            assert handler.update_plugin_repos() is False
        with open(cache) as f:
            assert json.load(f) == STALE_CATALOG

    def test_unserialisable_data_leaves_the_previous_cache_intact(self, handler):
        cache = _seed_cache(handler, 'default', STALE_CATALOG)
        payload = {'repo': {'name': 'Official'}, 'plugins': {object()}}
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data', return_value=payload):
            assert handler.update_plugin_repos() is False
        with open(cache) as f:
            assert json.load(f) == STALE_CATALOG

    def test_unserialisable_data_leaves_no_temporary_files(self, handler, tmp_path):
        _seed_cache(handler, 'default', STALE_CATALOG)
        payload = {'plugins': {object()}}
        with _single_repo(handler), \
                mock.patch.object(handler, 'fetch_remote_repo_data', return_value=payload):
            handler.update_plugin_repos()
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith('.tmp')] == []


class TestMultipleRepos:

    def test_one_bad_repo_does_not_block_the_others(self, handler):
        good_path = 'https://mirror.example.com/repo.json'
        _seed_cache(handler, 'default', STALE_CATALOG)

        def fetch(repo_path):
            return None if repo_path == 'default' else GOOD_CATALOG

        with mock.patch.object(handler, 'get_plugin_repos',
                               return_value=[{'path': 'default'}, {'path': good_path}]), \
                mock.patch.object(handler, 'fetch_remote_repo_data', side_effect=fetch):
            # Aggregate result is a failure...
            assert handler.update_plugin_repos() is False
        # ...but the healthy repo was still refreshed and the failed repo kept
        # its last known good catalog.
        with open(_cache_file(handler, good_path)) as f:
            assert json.load(f) == GOOD_CATALOG
        with open(_cache_file(handler, 'default')) as f:
            assert json.load(f) == STALE_CATALOG
