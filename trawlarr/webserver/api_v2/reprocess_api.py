#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    reprocess_api.py

    The API for deliberately re-running an existing library under changed
    pipeline rules (issue #41). All of the reasoning about what this does and
    why it is shaped the way it is lives in trawlarr/libs/reprocess.py; this
    module is the transport.

    Two endpoints, in the order they are meant to be used:

      POST /reprocess/preview   what would this filter act on?
      POST /reprocess/apply     act on it, quoting the count the preview gave

    The split is not politeness. `/reprocess/apply` refuses any request whose
    `confirm_count` and `confirm_digest` do not both match what the same filter
    selects at that moment, so `/reprocess/preview` is not an optional first
    step - it is the only way to learn the two values the second call has to
    quote. The digest is there because a count alone cannot tell "the 12 files
    you previewed" apart from "12 files, one of which you have never seen".

    Neither endpoint queues anything. `/reprocess/apply` deletes recorded
    state, and the next library scan (or `/pending/rescan`) is what actually
    queues the files.
"""

import tornado.log

from trawlarr.libs import reprocess
from trawlarr.webserver.api_v2.base_api_handler import BaseApiHandler, BaseApiError
from trawlarr.webserver.api_v2.schema.schemas import ReprocessAppliedResultSchema, ReprocessSelectionSchema, \
    RequestReprocessApplySchema, RequestReprocessSelectionSchema


class ApiReprocessHandler(BaseApiHandler):
    config = None
    params = None

    routes = [
        {
            "path_pattern":      r"/reprocess/preview",
            "supported_methods": ["POST"],
            "call_method":       "preview_reprocess_selection",
        },
        {
            "path_pattern":      r"/reprocess/apply",
            "supported_methods": ["POST"],
            "call_method":       "apply_reprocess_selection",
        },
    ]

    def initialize(self, **kwargs):
        self.params = kwargs.get("params")

    @staticmethod
    def _filter_kwargs(json_request):
        return {
            'library_id':        json_request.get('library_id'),
            'path_glob':         json_request.get('path_glob'),
            'match_file_test':   json_request.get('match_file_test', False),
            'include_failed':    json_request.get('include_failed', False),
            'clear_convergence': json_request.get('clear_convergence', False),
            'force':             json_request.get('force', False),
            'cooldown_hours':    json_request.get('cooldown_hours', reprocess.DEFAULT_COOLDOWN_HOURS),
            'limit':             json_request.get('limit', reprocess.DEFAULT_LIST_LIMIT),
        }

    async def preview_reprocess_selection(self):
        """
        Reprocess - preview what a selection would invalidate
        ---
        description: |
            Returns the files a reprocess request would act on, and the files it would not,
            with a reason for every one it would not. Nothing is changed.

            Completed-task history is what stops a file being processed a second time. This
            operation deletes that history for a chosen set of files so the ordinary pipeline
            picks them up again under the current rules - which is exactly the thing the
            platform otherwise goes to some trouble to prevent, so it is scoped, previewed
            and recorded rather than offered as a switch.

            A request must be scoped by `library_id`, `path_glob`, or both; an unscoped
            request is refused with 400, and so is one whose only scope is a `path_glob`
            made entirely of wildcards, because that matches every path there is. So is a
            filter matching more files than the operation will consider at once - it is
            refused rather than truncated.

            The `counts.selected` and `digest` values returned here are what
            `/reprocess/apply` requires as its `confirm_count` and `confirm_digest`.
        requestBody:
            description: The filter to preview.
            required: True
            content:
                application/json:
                    schema:
                        RequestReprocessSelectionSchema
        responses:
            200:
                description: 'Sample response: Returns what the filter would act on.'
                content:
                    application/json:
                        schema:
                            ReprocessSelectionSchema
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
            json_request = self.read_json_request(RequestReprocessSelectionSchema())
            selection = reprocess.build_selection(**self._filter_kwargs(json_request))
            self.write_success(self.build_response(ReprocessSelectionSchema(), selection))
            return
        except reprocess.ReprocessRefused as refused:
            # Understood, deliberately not done, nothing changed. That is a
            # 400 with the explanation, not a 500.
            self.set_status(self.STATUS_ERROR_EXTERNAL, reason=str(refused))
            self.write_error()
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()

    async def apply_reprocess_selection(self):
        """
        Reprocess - invalidate the completed state for a selection
        ---
        description: |
            Deletes the recorded completed state for every file the filter selects, so that
            the next library scan tests them again under the current rules.

            This endpoint queues nothing. It removes records; discovery, the file test and
            every plugin vote then run exactly as they do for a newly discovered file, so a
            file the current rules do not want is not processed just because it was selected
            here.

            `confirm_count` and `confirm_digest` must equal the `counts.selected` and
            `digest` values returned by `/reprocess/preview` for the same filter. If the
            selection has changed in the meantime - in size OR in membership - the request
            is refused and nothing is changed.

            Files reprocessed within the last `cooldown_hours` are skipped unless `force` is
            set, and every applied request is written to a per-file audit trail carrying a
            count - repeatedly invalidating the same files is what a reprocess loop looks
            like, and this is what makes one visible.
        requestBody:
            description: The filter to act on, and the count and digest the preview reported.
            required: True
            content:
                application/json:
                    schema:
                        RequestReprocessApplySchema
        responses:
            200:
                description: 'Sample response: Returns what was invalidated.'
                content:
                    application/json:
                        schema:
                            ReprocessAppliedResultSchema
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
            json_request = self.read_json_request(RequestReprocessApplySchema())
            result = reprocess.apply_selection(json_request.get('confirm_count'),
                                               confirm_digest=json_request.get('confirm_digest'),
                                               **self._filter_kwargs(json_request))
            self.write_success(self.build_response(ReprocessAppliedResultSchema(), result))
            return
        except reprocess.ReprocessRefused as refused:
            self.set_status(self.STATUS_ERROR_EXTERNAL, reason=str(refused))
            self.write_error()
            return
        except BaseApiError as bae:
            tornado.log.app_log.error("BaseApiError.{}: {}".format(self.route.get('call_method'), str(bae)))
            return
        except Exception as e:
            self.set_status(self.STATUS_ERROR_INTERNAL, reason=str(e))
            self.write_error()
