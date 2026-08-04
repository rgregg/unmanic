#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    activity_api.py

    A single, stable, machine-readable answer to "is Trawlarr busy?", meant to
    be gated on by external maintenance tooling that shares storage or a GPU
    with this installation.

    Why this is not just `/workers/status`: that endpoint reports per-worker
    idleness and nothing else. It has no queue depth, its payload is a moving
    target of log tails and plugin runner internals, and - most importantly -
    every worker reports idle for the whole post-processing window, which is
    when Trawlarr does its large file copies and source deletions. A gate
    built on it is a gate that opens while files are still being written.
"""


from trawlarr.webserver.api_v2.base_api_handler import BaseApiHandler, BaseApiError
from trawlarr.webserver.api_v2.schema.schemas import ActivityStatusSuccessSchema
from trawlarr.webserver.helpers import activity


class ApiActivityHandler(BaseApiHandler):
    config = None
    params = None

    routes = [
        {
            "path_pattern":      r"/activity/status",
            "supported_methods": ["GET"],
            "call_method":       "activity_status",
        },
    ]

    def initialize(self, **kwargs):
        self.params = kwargs.get("params")

    async def activity_status(self):
        """
        Activity - Return whether this installation is currently doing work
        ---
        description: |
            Returns whether Trawlarr may currently be touching library files, together with
            the worker and task queue counts that answer was derived from.

            This is the supported gate for external tooling that shares storage or hardware
            with Trawlarr. `busy` is False only when every worker is idle and there are no
            pending, in-progress or post-processing tasks. It is intentionally conservative:
            post-processing file moves run after a worker reports idle, so worker state on
            its own is not safe to gate on.

            If the activity state cannot be determined - for example while the Foreman thread
            is not running - this returns 500. It never reports an unknown state as idle.
        responses:
            200:
                description: 'Sample response: Returns the current activity state.'
                content:
                    application/json:
                        schema:
                            ActivityStatusSuccessSchema
            400:
                description: Bad request; Check `messages` for any validation errors
                content:
                    application/json:
                        schema:
                            BadRequestSchema
            404:
                description: Bad request; Requested endpoint not found
                content:
                    application/json:
                        schema:
                            BadEndpointSchema
            405:
                description: Bad request; Requested method is not allowed
                content:
                    application/json:
                        schema:
                            BadMethodSchema
            500:
                description: Internal error; Check `error` for exception
                content:
                    application/json:
                        schema:
                            InternalErrorSchema
        """
        try:
            response = self.build_response(
                ActivityStatusSuccessSchema(),
                activity.get_activity_status()
            )
            self.write_success(response)
            return
        except BaseApiError as bae:
            self.handle_api_error(bae)
            return
        except Exception as e:
            self.handle_unexpected_error(e)
