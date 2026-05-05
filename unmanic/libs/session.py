#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
unmanic.session.py

Written by:               Josh.5 <jsunnex@gmail.com>
Date:                     10 Mar 2021, (5:20 PM)

Copyright:
       Copyright (C) Josh Sunnex - All Rights Reserved

       Permission is hereby granted, free of charge, to any person obtaining a copy
       of this software and associated documentation files (the "Software"), to deal
       in the Software without restriction, including without limitation the rights
       to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
       copies of the Software, and to permit persons to whom the Software is
       furnished to do so, subject to the following conditions:

       The above copyright notice and this permission notice shall be included in all
       copies or substantial portions of the Software.

       THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
       EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
       MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
       IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
       DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
       OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
       OR OTHER DEALINGS IN THE SOFTWARE.

"""
import datetime
import os
import random
import threading
import time
from urllib.parse import urlparse

import requests

from unmanic import config
from unmanic.libs.logs import UnmanicLogging
from unmanic.libs.singleton import SingletonType
from unmanic.libs.unmodels import Installation

# Local fork: every install runs at the highest supporter tier. This pin
# replaces the remote level lookup that used to come from
# api.unmanic.app/support-auth-api/v2/user_info/get and removes the only
# reason the application had to phone home on a schedule. Override with
# UNMANIC_LOCAL_SESSION_LEVEL if a different level is needed for testing.
LOCAL_SESSION_LEVEL = int(os.environ.get("UNMANIC_LOCAL_SESSION_LEVEL", "7"))


class RemoteApiException(Exception):
    """
    RemoteApiException
    Custom exception for errors contacting the remote Unmanic API
    """

    def __init__(self, message, status_code):
        super().__init__(f"Session Error - Remote API [CODE: {status_code}]: {message}")


class InvalidApplicationTokenException(Exception):
    """
    InvalidApplicationTokenException
    Raised when the application token is explicitly rejected by auth API.
    """

    def __init__(self, message, status_code):
        super().__init__(f"Session Error - Invalid Application Token [CODE: {status_code}]: {message}")


class Session(object, metaclass=SingletonType):
    """
    Session

    Manages the Unmanic applications session for unlocking
    features and fetching data from the Unmanic site API.

    """

    """
    level - The user auth level
    Set level to 0 by default
    """
    level = 0

    """
    non supporter library count
    """
    library_count = 2

    """
    non supporter linked installations count
    """
    link_count = 3

    """
    picture_uri - The user avatar
    """
    picture_uri = ""

    """
    name - The user's name
    """
    name = ""

    """
    email - The user's email
    """
    email = ""

    """
    created - The timestamp when the session was created
    """
    created = None

    """
    last_check - The timestamp when the session was last checked
    """
    last_check = None

    """
    uuid - This installation's UUID
    """
    uuid = None

    """
    user_access_token - The access token to authenticate requests with the Unmanic API
    """
    user_access_token = None

    """
    application_token - The application token for acquiring an updated access token
    """
    application_token = None

    """
    background thread used to refresh cached plugin repos when supporter level changes
    """
    plugin_repo_refresh_thread = None

    """
    background retry thread used when the level changes during an active repo refresh
    """
    plugin_repo_refresh_retry_thread = None

    def __init__(self, *args, **kwargs):
        self.logger = UnmanicLogging.get_logger(name=__class__.__name__)
        self.timeout = 30
        self.dev_api = kwargs.get("dev_api", None)
        self.requests_session = requests.Session()
        self.token_poll_task = None
        self.logger.info("Initialising new session object")
        self.created = None
        self.last_check = None

    @staticmethod
    def __normalise_token(token):
        """
        Normalise persisted token values so legacy stringified nulls do not
        get treated as valid tokens.
        """
        if token is None:
            return None
        token = str(token).strip()
        if token.lower() in ["", "none", "null"]:
            return None
        return token

    def __created_older_than_x_days(self, days=1):
        if not self.created:
            # There is no session created. How did we get here???
            return False
        # (86400 = 24 hours)
        seconds = days * 86400
        # Get session expiration time
        time_now = time.time()
        time_when_session_expires = self.created + seconds
        # Check that the time create is less than X days old
        if time_now < time_when_session_expires:
            return False
        return True

    def __check_session_valid(self):
        """
        Ensure that the session is valid.
        A session is only valid for a limited amount of time.
        After a session expires, it should be re-acquired.

        :return:
        """
        # Last checked less than a min ago... just keep the current session.
        # This check is only to prevent spamming requests when the site API is unreachable
        # Only check in every 40 mins (2400s) minimum. Never ignore a checkin for more than 45 mins (2700s)
        if self.last_check and (time.time() - self.last_check) < 2400:
            return True
        # If the session has never been created, return false
        if not self.created:
            return False
        # Check if the time the session was created is less than X days old
        if not self.__created_older_than_x_days(days=2):
            # Only try to recreate the session once a day
            return True
        self.logger.debug("Session no longer valid")
        return False

    def __update_created_timestamp(self):
        """
        Update the session "created" timestamp.

        :return:
        """
        # Get the time now in seconds
        seconds = time.time()
        # Create a seconds offset of some random number between 300 (5 mins) and 900 (15 mins)
        seconds_offset = random.randint(300, 900)
        # Set the created flag with the seconds variable plus a random offset to avoid people joining
        #   together to register if the site goes down
        self.created = seconds + seconds_offset
        # Print only the accurate update time in debug log
        created = datetime.datetime.fromtimestamp(seconds)
        self.logger.debug("Updated session at %s", str(created))

    def __fetch_installation_data(self):
        """
        Fetch installation data from DB

        :return:
        """
        # Fetch installation
        db_installation = Installation()
        try:
            # Fetch a single row (get() will raise DoesNotExist exception if no results are found)
            current_installation = db_installation.select().order_by(Installation.id.asc()).limit(1).get()
        except Exception:
            # Create settings (defaults will be applied)
            self.logger.debug("Unmanic session does not yet exist... Creating.")
            db_installation.delete().execute()
            current_installation = db_installation.create()

        self.uuid = str(current_installation.uuid)
        self.level = int(current_installation.level)
        self.picture_uri = str(current_installation.picture_uri)
        self.name = str(current_installation.name)  # This is the user's name. Not the installation's name
        self.email = str(current_installation.email)
        self.created = current_installation.created if current_installation.created else None
        if isinstance(self.created, datetime.datetime):
            self.created = self.created.timestamp()

        self.application_token = self.__normalise_token(current_installation.application_token)
        self.__update_session_auth(access_token=current_installation.user_access_token)

    def __store_installation_data(self, force_save_access_token=False):
        """
        Store installation data in DB to persist reboot

        :return:
        """
        if self.uuid:
            db_installation = Installation.get_or_none(uuid=self.uuid)
            db_installation.level = self.level
            db_installation.picture_uri = self.picture_uri
            db_installation.name = self.name  # This is the user's name. Not the installation's name
            db_installation.email = self.email
            db_installation.created = self.created
            if self.user_access_token or force_save_access_token:
                db_installation.user_access_token = self.user_access_token
            if self.application_token or force_save_access_token:
                db_installation.application_token = self.application_token
            db_installation.save()

    def __refresh_plugin_repos_for_level_change(self, previous_level, new_level, source):
        try:
            from unmanic.libs.plugins import PluginsHandler

            self.logger.info(
                "Refreshing plugin repos after supporter level change %s -> %s (source=%s)",
                previous_level,
                new_level,
                source,
            )
            plugin_handler = PluginsHandler()
            plugin_handler.update_plugin_repos()
            self.logger.info(
                "Plugin repo refresh completed after supporter level change %s -> %s (source=%s)",
                previous_level,
                new_level,
                source,
            )
        except Exception as e:
            self.logger.error(
                "Failed to refresh plugin repos after supporter level change %s -> %s (source=%s). %s",
                previous_level,
                new_level,
                source,
                e,
            )
        finally:
            self.plugin_repo_refresh_thread = None

    def __retry_plugin_repo_refresh_for_level_change(self, previous_level, new_level, source, delay_seconds=5):
        time.sleep(delay_seconds)
        self.plugin_repo_refresh_retry_thread = None
        self.__trigger_plugin_repo_refresh_for_level_change(
            previous_level,
            new_level,
            f"{source}_retry",
        )

    def __trigger_plugin_repo_refresh_for_level_change(self, previous_level, new_level, source):
        if int(previous_level) == int(new_level):
            return

        existing_thread = self.plugin_repo_refresh_thread
        if existing_thread and existing_thread.is_alive():
            self.logger.info(
                "Supporter level changed %s -> %s (source=%s) while a plugin repo refresh is already running. "
                "Scheduling delayed retry.",
                previous_level,
                new_level,
                source,
            )
            retry_thread = self.plugin_repo_refresh_retry_thread
            if retry_thread and retry_thread.is_alive():
                return
            retry_thread = threading.Thread(
                target=self.__retry_plugin_repo_refresh_for_level_change,
                args=(previous_level, new_level, source),
                name="SessionLevelPluginRepoRefreshRetry",
                daemon=True,
            )
            self.plugin_repo_refresh_retry_thread = retry_thread
            retry_thread.start()
            return

        refresh_thread = threading.Thread(
            target=self.__refresh_plugin_repos_for_level_change,
            args=(previous_level, new_level, source),
            name="SessionLevelPluginRepoRefresh",
            daemon=True,
        )
        self.plugin_repo_refresh_thread = refresh_thread
        refresh_thread.start()

    def __configure_log_forwarding(self, session_valid=False):
        # Local fork: never forward logs to a remote datastore. The endpoint
        # lookup used to call api.unmanic.app's central_config service; an
        # explicit UNMANIC_REMOTE_LOGGING_ENDPOINT env var still works for
        # users who want to point at their own log sink.
        settings = config.Config()
        log_buffer_retention = settings.get_log_buffer_retention()
        if session_valid:
            endpoint = os.environ.get("UNMANIC_REMOTE_LOGGING_ENDPOINT", "")
            if endpoint and endpoint.startswith("http"):
                UnmanicLogging.enable_remote_logging(endpoint, self.uuid, log_buffer_retention)
                return
        UnmanicLogging.disable_remote_logging(log_buffer_retention)

    def __sync_remote_installation_addresses(self):
        """
        Local fork: no phone-home. Linked installation addresses are managed
        locally only.
        """
        return

    def __reset_session_installation_data(self):
        """
        Reset stored session data

        :return:
        """
        self.logger.debug("Resetting session installation data.")
        previous_level = self.level
        self.level = 0
        self.picture_uri = ""
        self.name = ""
        self.email = ""
        self.created = time.time()
        self.user_access_token = None
        self.application_token = None
        self.__store_installation_data(force_save_access_token=True)
        self.__configure_log_forwarding(session_valid=False)
        self.__clear_session_auth()
        self.__trigger_plugin_repo_refresh_for_level_change(previous_level, self.level, "reset_session")

    def __update_session_auth(self, access_token=None):
        # Update session headers
        token = self.__normalise_token(access_token)
        self.user_access_token = token
        self.requests_session.headers.update({"Authorization": token or ""})

    def __clear_session_auth(self):
        self.requests_session.cookies.clear()
        self.requests_session.headers.update({"Authorization": ""})

    def revoke_access_token(self, reason=""):
        """
        Revoke the current access token so the next authenticated request is
        forced to fetch a fresh token from support-auth-api.
        """
        self.logger.info(
            "Revoking cached access token%s",
            f" ({reason})" if reason else "",
        )
        self.user_access_token = None
        self.requests_session.headers.update({"Authorization": ""})
        self.__store_installation_data(force_save_access_token=True)

    def get_installation_uuid(self):
        """
        Returns the installation UUID as a string.
        If it does not yet exist, it will create one.

        :return:
        """
        if not self.uuid:
            self.__fetch_installation_data()
        return self.uuid

    def get_supporter_level(self):
        """
        Returns the supporter level

        :return:
        """
        if not self.level:
            self.__fetch_installation_data()
        return self.level

    def get_site_url(self):
        """
        Set the Unmanic application site URL
        :return:
        """
        api_proto = "https"
        api_domain = "api.unmanic.app"
        if self.dev_api:
            return self.dev_api
        return "{0}://{1}".format(api_proto, api_domain)

    def set_full_api_url(self, api_prefix, api_version, api_path):
        """
        Set the API path URL

        :param api_prefix:
        :param api_version:
        :param api_path:
        :return:
        """
        api_versioned_path = "{}/v{}".format(api_prefix, api_version)
        return "{0}/{1}/{2}".format(self.get_site_url(), api_versioned_path, api_path)

    def api_get(self, api_prefix, api_version, api_path):
        """
        Generate and execute a GET API call.

        :param api_prefix:
        :param api_version:
        :param api_path:
        :return:
        """
        u = self.set_full_api_url(api_prefix, api_version, api_path)
        r = self.requests_session.get(u, timeout=self.timeout)
        if r.status_code > 403:
            # There is an issue with the remote API
            raise RemoteApiException(f"GET request failed for {u}", r.status_code)
        if r.status_code == 401:
            # Verify the token. Refresh as required
            self.logger.debug("Auto exec token verification (api_get)")
            token_verified = self.verify_token()
            # If successful, then retry request
            if token_verified:
                r = self.requests_session.get(u, timeout=self.timeout)
                if r.status_code > 403:
                    # There is an issue with the remote API
                    raise RemoteApiException(f"GET request still failed for {u}", r.status_code)
            else:
                self.logger.debug("Failed to verify auth (api_get)")
        return r.json(), r.status_code

    def api_post(self, api_prefix, api_version, api_path, data):
        """
        Generate and execute a POST API call.

        :param api_prefix:
        :param api_version:
        :param api_path:
        :param data:
        :return:
        """
        u = self.set_full_api_url(api_prefix, api_version, api_path)
        r = self.requests_session.post(u, json=data, timeout=self.timeout)
        if r.status_code > 403:
            # There is an issue with the remote API
            raise RemoteApiException(f"POST request failed for {u}", r.status_code)
        if r.status_code == 401:
            # Verify the token. Refresh as required
            self.logger.debug("Auto exec token verification (api_post)")
            token_verified = self.verify_token()
            # If successful, then retry request
            if token_verified:
                r = self.requests_session.post(u, json=data, timeout=self.timeout)
                if r.status_code > 403:
                    # There is an issue with the remote API
                    raise RemoteApiException(f"POST request still failed for {u}", r.status_code)
            else:
                self.logger.debug("Failed to verify auth (api_post)")
        return r.json(), r.status_code

    def get_access_token(self):
        # Local fork: no phone-home. Always succeeds, no token needed.
        return True

    def verify_token(self):
        # Local fork: no phone-home. Tokens are never validated remotely.
        return True

    def fetch_user_data(self):
        # Local fork: no phone-home. User identity stays at whatever is in
        # the local DB; supporter level is held at LOCAL_SESSION_LEVEL by
        # register_unmanic so all features stay unlocked.
        return

    def auth_user_account(self, force_checkin=False):
        # Local fork: no phone-home. Auth always succeeds.
        return True

    def auth_trial_account(self):
        # Local fork: no phone-home. Trial flow is unused.
        return True

    def register_unmanic(self, force=False):
        """
        Local fork: no-op stub for the upstream registration call. Loads the
        local installation row from the DB, pins the session level to
        LOCAL_SESSION_LEVEL so every feature stays unlocked, and refreshes
        the timestamp so __check_session_valid() never expires.
        """
        self.last_check = time.time()
        self.__fetch_installation_data()
        previous_level = self.level
        self.level = LOCAL_SESSION_LEVEL
        if not self.created:
            self.__update_created_timestamp()
        self.__store_installation_data()
        self.__configure_log_forwarding(session_valid=True)
        if previous_level != self.level:
            self.__trigger_plugin_repo_refresh_for_level_change(
                previous_level, self.level, "self_hosted_init")
        return True

    def sign_out(self, remote=True):
        """
        Remove any user auth. Local fork: never calls the remote logout
        endpoint; the local DB row is wiped and register_unmanic will
        re-pin the level on the next call.
        """
        self.__reset_session_installation_data()
        return True

    def get_sign_out_url(self):
        """
        Fetch the application sign out URL

        :return:
        """
        return "{0}/unmanic-api/v1/installation_auth/logout".format(self.get_site_url())

    def init_device_auth_flow(self):
        """
        Local fork: device-flow login is unused. The session is pinned to
        LOCAL_SESSION_LEVEL via register_unmanic, so there is nothing to
        log into. Returning False keeps any UI button no-op without
        contacting api.unmanic.app.
        """
        self.logger.info("Local fork: device-flow login is disabled. Session level is pinned locally.")
        return False

    def poll_for_app_token(self, device_code, interval, expires_in):
        # Local fork: device-flow login is unused. See init_device_auth_flow.
        return None

    def get_patreon_login_url(self):
        """
        Fetch the Patreon Login URL

        :return:
        """
        return "{0}/support-auth-api/v1/login_patreon/login".format(self.get_site_url())

    def get_github_login_url(self):
        """
        Fetch the GitHub Login URL

        :return:
        """
        return "{0}/support-auth-api/v1/login_github/login".format(self.get_site_url())

    def get_discord_login_url(self):
        """
        Fetch the Discord Login URL

        :return:
        """
        return "{0}/support-auth-api/v1/login_discord/login".format(self.get_site_url())

    def get_patreon_sponsor_page(self):
        # Local fork: no phone-home. The upstream Patreon link still works
        # if the user wants to support upstream — they can find it on
        # unmanic.app — but this app does not fetch it.
        return False

    def get_credit_portal_funding_proposals(self):
        # Local fork: no phone-home. Funding proposals were a supporter-tier
        # feature on api.unmanic.app; not applicable here.
        return None, 200
