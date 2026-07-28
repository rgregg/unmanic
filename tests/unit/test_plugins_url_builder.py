#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_plugins_url_builder.py

    Tests for PluginsHandler.get_plugins_in_repo_data — the URL builder
    that converts a fetched catalog into per-plugin package_url /
    changelog_url. This is the codepath that lets the fork pull plugin
    zips directly from raw.githubusercontent.com instead of through
    api.unmanic.app's proxy: when the catalog has `repo_data_directory`
    set, URLs resolve to that base + plugin_id + version.

    The integration story: feat/direct-plugin-repo-fetch fetches the
    catalog directly from GitHub, the catalog's `repo_data_directory`
    survives intact (the unmanic.app proxy used to strip it), this
    function builds direct GitHub URLs from it, and download_plugin
    just GETs that URL with no auth header.
"""
import logging
from unittest import mock

import pytest

from unmanic.libs.plugins import PluginsHandler


def _bare_handler():
    h = PluginsHandler.__new__(PluginsHandler)
    h.logger = logging.getLogger("test")
    h.version = 2  # PluginsHandler.version, the plugin handler version
    return h


def _public_catalog_shape(plugins):
    """A catalog shaped like the official Unmanic/unmanic-plugins repo
    branch's repo.json (the public format we now consume directly)."""
    return {
        "repo": {
            "id": "repository.official",
            "name": "Official Repo",
            "icon": "",
            "repo_data_directory": "https://raw.githubusercontent.com/Unmanic/unmanic-plugins/repo/",
            "repo_data_url": "https://raw.githubusercontent.com/Unmanic/unmanic-plugins/repo/repo.json",
        },
        "plugins": plugins,
    }


def _proxy_catalog_shape(plugins):
    """A catalog shaped like what the api.unmanic.app proxy used to
    return: no repo_data_directory, every plugin has a per-user
    proxy URL in plugin_download_url."""
    return {
        "repo": {
            "id": "repository.official",
            "name": "Official Repo",
            "icon": "",
        },
        "plugins": plugins,
    }


class TestDirectGitHubCatalog:
    """The catalog format the fork now consumes — repo_data_directory
    set, no per-plugin URL needed. Plugin URLs resolve to GitHub raw
    paths."""

    def test_builds_direct_github_zip_urls(self):
        h = _bare_handler()
        catalog = _public_catalog_shape([
            {"id": "video_transcoder", "version": "0.1.18",
             "name": "VT", "compatibility": [2]},
        ])
        with mock.patch.object(h, "get_plugin_info", return_value=None):
            result = h.get_plugins_in_repo_data(catalog)
        assert len(result) == 1
        assert result[0]["package_url"] == (
            "https://raw.githubusercontent.com/Unmanic/unmanic-plugins/repo/"
            "video_transcoder/video_transcoder-0.1.18.zip")

    def test_builds_changelog_url(self):
        h = _bare_handler()
        catalog = _public_catalog_shape([
            {"id": "discord_webhook", "version": "1.0.0",
             "name": "DW", "compatibility": [2]},
        ])
        with mock.patch.object(h, "get_plugin_info", return_value=None):
            result = h.get_plugins_in_repo_data(catalog)
        assert result[0]["changelog_url"] == (
            "https://raw.githubusercontent.com/Unmanic/unmanic-plugins/repo/"
            "discord_webhook/changelog.md")

    def test_repo_data_directory_trailing_slash_normalised(self):
        """The catalog might publish the directory with or without a
        trailing slash; the URL builder must produce the same result."""
        h = _bare_handler()
        catalog = _public_catalog_shape([
            {"id": "p", "version": "1.0", "name": "P", "compatibility": [2]},
        ])
        catalog["repo"]["repo_data_directory"] = "https://example.com/repo/"
        with mock.patch.object(h, "get_plugin_info", return_value=None):
            r = h.get_plugins_in_repo_data(catalog)
        assert r[0]["package_url"] == "https://example.com/repo/p/p-1.0.zip"

    def test_repo_data_directory_no_trailing_slash_works(self):
        h = _bare_handler()
        catalog = _public_catalog_shape([
            {"id": "p", "version": "1.0", "name": "P", "compatibility": [2]},
        ])
        catalog["repo"]["repo_data_directory"] = "https://example.com/repo"
        with mock.patch.object(h, "get_plugin_info", return_value=None):
            r = h.get_plugins_in_repo_data(catalog)
        assert r[0]["package_url"] == "https://example.com/repo/p/p-1.0.zip"

    def test_incompatible_plugins_filtered_out(self):
        """Plugins must declare compatibility with the running plugin
        handler version (2). Skip those that don't."""
        h = _bare_handler()
        catalog = _public_catalog_shape([
            {"id": "old", "version": "1", "name": "Old", "compatibility": [1]},
            {"id": "new", "version": "1", "name": "New", "compatibility": [2]},
        ])
        with mock.patch.object(h, "get_plugin_info", return_value=None):
            r = h.get_plugins_in_repo_data(catalog)
        assert [p["plugin_id"] for p in r] == ["new"]


class TestProxyShapedCatalog:
    """The legacy/proxy-shaped catalog (no repo_data_directory) still
    works via the per-plugin plugin_download_url fallback. Important
    for backwards compat with any third-party catalog that hasn't
    moved to the direct format."""

    def test_uses_plugin_download_url_when_no_repo_data_directory(self):
        h = _bare_handler()
        catalog = _proxy_catalog_shape([
            {"id": "p", "version": "1.0", "name": "P",
             "compatibility": [2],
             "plugin_download_url": "https://example.com/some/path/p-1.0.zip"},
        ])
        with mock.patch.object(h, "get_plugin_info", return_value=None):
            r = h.get_plugins_in_repo_data(catalog)
        assert r[0]["package_url"] == "https://example.com/some/path/p-1.0.zip"
        # Changelog URL is empty in this format — proxy didn't expose one.
        assert r[0]["changelog_url"] == ""


class TestInstalledStatus:

    def test_marks_installed_when_local_version_matches(self):
        h = _bare_handler()
        catalog = _public_catalog_shape([
            {"id": "p", "version": "1.0", "name": "P", "compatibility": [2]},
        ])
        with mock.patch.object(h, "get_plugin_info", return_value={"version": "1.0"}):
            r = h.get_plugins_in_repo_data(catalog)
        assert r[0]["status"] == {"installed": True, "update_available": False}

    def test_flags_update_available_when_versions_differ(self):
        h = _bare_handler()
        catalog = _public_catalog_shape([
            {"id": "p", "version": "1.1", "name": "P", "compatibility": [2]},
        ])
        with mock.patch.object(h, "get_plugin_info", return_value={"version": "1.0"}), \
                mock.patch.object(h, "flag_plugin_for_update_by_id") as flag:
            r = h.get_plugins_in_repo_data(catalog)
        assert r[0]["status"] == {"installed": True, "update_available": True}
        flag.assert_called_once_with("p")

    def test_uninstalled_when_get_plugin_info_returns_none(self):
        h = _bare_handler()
        catalog = _public_catalog_shape([
            {"id": "p", "version": "1.0", "name": "P", "compatibility": [2]},
        ])
        with mock.patch.object(h, "get_plugin_info", return_value=None):
            r = h.get_plugins_in_repo_data(catalog)
        assert r[0]["status"] == {"installed": False}
