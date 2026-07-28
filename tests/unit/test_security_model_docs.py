#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_security_model_docs.py

    Tripwires for the claims made in docs/SECURITY_MODEL.md.

    The document states flatly that Trawlarr authenticates nothing, that
    the shipped reverse-proxy example keeps port 8888 off the host, and
    that the TLS test fixture is loopback-only. If any of that stops being
    true — someone lands authentication, or edits an example compose file
    to publish the app port — these fail so the documentation is updated
    in the same change rather than quietly becoming a lie.
"""
import os

import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _read(*relpath):
    with open(os.path.join(PROJECT_ROOT, *relpath), 'r', encoding='utf-8') as f:
        return f.read()


def _load_yaml(*relpath):
    return yaml.safe_load(_read(*relpath))


class TestDocumentedAuthBehaviour:
    """
    Pin the "no inbound authentication" claim to the code it describes.
    """

    def test_tornado_settings_do_not_enable_xsrf_or_cookie_secret(self):
        # SECURITY_MODEL.md states there is no CSRF protection and no
        # session cookie. Both are Tornado application settings; if either
        # appears, the document is out of date.
        from trawlarr.libs.uiserver import tornado_settings

        assert 'xsrf_cookies' not in tornado_settings
        assert 'cookie_secret' not in tornado_settings

    def test_ui_server_module_defines_no_authentication_hook(self):
        # There is no login handler and no `@authenticated` route anywhere
        # in the request path. Grep the server module rather than the whole
        # tree: this is a tripwire, not a proof.
        source = _read('trawlarr', 'libs', 'uiserver.py')

        assert 'authenticated' not in source
        assert 'get_current_user' not in source

    def test_api_handlers_set_no_authentication_headers(self):
        # The only per-request header handling in the v2 API base handler
        # is the JSON content type. No auth challenge, no session cookie.
        source = _read('trawlarr', 'webserver', 'api_v2', 'base_api_handler.py')

        assert 'WWW-Authenticate' not in source
        assert 'set_secure_cookie' not in source


class TestReverseProxyExample:
    """
    The supported internet-facing pattern must actually be safe to copy.
    """

    def test_trawlarr_service_publishes_no_host_ports(self):
        compose = _load_yaml('docker', 'docker-compose-caddy.yml')
        trawlarr = compose['services']['trawlarr']

        # Any published port is a door that bypasses the proxy's auth.
        assert 'ports' not in trawlarr

    def test_only_the_proxy_publishes_ports(self):
        compose = _load_yaml('docker', 'docker-compose-caddy.yml')
        publishing = [
            name for name, service in compose['services'].items()
            if service.get('ports')
        ]

        assert publishing == ['caddy']

    def test_caddyfile_authenticates_every_route(self):
        caddyfile = _read('docker', 'Caddyfile.example')

        # `basic_auth *` covers the whole vhost. Scoping it to a path would
        # leave /trawlarr/api/* — which can install plugins — wide open.
        assert 'basic_auth *' in caddyfile
        assert 'reverse_proxy trawlarr:8888' in caddyfile


class TestTlsTestFixtureStaysLoopback:

    def test_ssl_compose_binds_to_loopback_only(self):
        compose = _load_yaml('docker', 'docker-compose-ssl.yml')
        # Renamed with the service in issue #49 step 5. The assertion that
        # matters is the loopback bind, not the name -- so read whichever
        # single service the fixture defines rather than pinning a key that
        # a later rename can silently turn into a KeyError-shaped pass.
        services = compose['services']
        assert len(services) == 1, 'TLS fixture should define exactly one service'
        ports = next(iter(services.values()))['ports']

        # Unauthenticated TLS test instance; must not be reachable off-host.
        for mapping in ports:
            assert str(mapping).startswith('127.0.0.1:')


class TestDocumentationIsDiscoverable:

    def test_security_doc_exists_and_is_linked_from_readme(self):
        assert os.path.isfile(os.path.join(PROJECT_ROOT, 'docs', 'SECURITY_MODEL.md'))

        readme = _read('README.md')
        assert 'docs/SECURITY_MODEL.md' in readme
        assert 'does not authenticate inbound requests' in readme

    def test_security_doc_links_resolve(self):
        doc = _read('docs', 'SECURITY_MODEL.md')

        for relpath in ('docker/docker-compose-caddy.yml',
                        'docker/Caddyfile.example',
                        'trawlarr/webserver/docs/privacy_policy.md'):
            assert '../{}'.format(relpath) in doc
            assert os.path.isfile(os.path.join(PROJECT_ROOT, *relpath.split('/')))
