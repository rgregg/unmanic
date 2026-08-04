#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_api_contract_drift.py

    The v2 OpenAPI contract is generated from the handler docstrings and
    checked in under trawlarr/webserver/docs/. It is also served to the browser
    from those files, so a stale copy is a lie told to every API consumer.

    These tests fail when the checked-in JSON/YAML no longer matches what the
    handlers describe. Regenerate with:

        python -c "from trawlarr.webserver.api_v2.schema.swagger import \\
            generate_swagger_file; generate_swagger_file()"

    The retired-endpoint assertions are the documentation half of #21: the
    contract must advertise 410 (and only 410) for the retired central
    account, authentication and funding routes.
"""
import json

import pytest
import yaml

from trawlarr.webserver.api_v2.schema.swagger import build_swagger_spec, get_swagger_file_location

RETIRED_PATHS = [
    '/session/logout',
    '/session/get_app_auth_code',
    '/session/funding_proposals',
]


@pytest.fixture(scope='module')
def generated_spec():
    spec, _errors = build_swagger_spec()
    return spec.to_dict()


@pytest.fixture(scope='module')
def checked_in_json():
    with open('{}.json'.format(get_swagger_file_location()), encoding='utf-8') as file:
        return json.load(file)


@pytest.fixture(scope='module')
def checked_in_yaml():
    with open('{}.yaml'.format(get_swagger_file_location()), encoding='utf-8') as file:
        return yaml.safe_load(file)


class TestContractIsNotStale:

    def test_checked_in_json_matches_the_handlers(self, generated_spec, checked_in_json):
        assert checked_in_json == generated_spec

    def test_checked_in_yaml_matches_the_handlers(self, generated_spec, checked_in_yaml):
        assert checked_in_yaml == generated_spec


class TestRetiredEndpointsAreDocumented:

    @pytest.mark.parametrize('path', RETIRED_PATHS)
    def test_only_410_is_advertised(self, checked_in_json, path):
        responses = checked_in_json['paths'][path]['get']['responses']
        assert list(responses.keys()) == ['410']

    @pytest.mark.parametrize('path', RETIRED_PATHS)
    def test_410_returns_the_retired_endpoint_schema(self, checked_in_json, path):
        schema = checked_in_json['paths'][path]['get']['responses']['410']['content']['application/json']['schema']
        assert schema == {'$ref': '#/components/schemas/RetiredEndpoint'}

    @pytest.mark.parametrize('path', RETIRED_PATHS)
    def test_description_says_it_is_retired(self, checked_in_json, path):
        description = checked_in_json['paths'][path]['get']['description']
        assert description.lower().startswith('retired')

    def test_retired_endpoint_schema_shape(self, checked_in_json):
        # #23 added error_code to every error envelope. The #21 contract is
        # unchanged underneath it: error, messages and the retired flag stay.
        schema = checked_in_json['components']['schemas']['RetiredEndpoint']
        assert sorted(schema['required']) == ['error', 'error_code', 'messages', 'retired']
        assert schema['properties']['retired']['type'] == 'boolean'

    def test_retired_endpoint_is_classified_as_retired_not_as_a_generic_error(self, checked_in_json):
        schema = checked_in_json['components']['schemas']['RetiredEndpoint']
        assert schema['properties']['error_code']['example'] == 'ENDPOINT_RETIRED'

    def test_device_auth_code_schema_is_gone(self, checked_in_json):
        """
        The device authentication flow is retired; its response schema must
        not linger in the published contract.
        """
        assert 'SessionAuthCode' not in checked_in_json['components']['schemas']
