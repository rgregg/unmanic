#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.libs.plugin_registration.py

    A read-only startup consistency check across the three tables a plugin
    must appear in before it will run. See issue #38.

    THE PROBLEM
    -----------
    Three tables decide whether a plugin executes against a library:

      plugins            registration - one row per installed plugin, keyed by
                         `plugin_id`, the plugin's DIRECTORY id.
      enabledplugins     per-library enablement. THIS is what gates execution;
                         the legacy global `plugins.enabled` flag does not.
      librarypluginflow  execution order within a library for one plugin type.

    When those disagree, nothing complains. The plugin simply never fires, the
    task still reports success, and the log says nothing at all. The whole
    point of this module is that the failure stops being invisible.

    WHAT IS REPORTED
    ----------------
    flow_without_enabled   A `librarypluginflow` row positions a plugin in a
                           library that has no `enabledplugins` row for it.
                           The ordering entry is a fossil; the plugin does not
                           run in that library. This is the issue's
                           "configured but not enabled".

    missing_plugin_record  An `enabledplugins` or `librarypluginflow` row
                           points at a `plugins` row id that does not exist.
                           Foreign keys are enforced with ON DELETE CASCADE
                           today, so this should be unreachable on a healthy
                           install - but a database that predates enforcement,
                           a partial restore, or a hand-edit can still produce
                           it, and the row is then dead weight forever.

    plugin_not_installed   A row resolves to a `plugins` record whose plugin is
                           not actually on disk. This is the loudest case,
                           because `PluginExecutor` drops a plugin it cannot
                           import WITHOUT COMMENT: the runner list simply comes
                           back one entry shorter. An interrupted uninstall, a
                           plugins volume that did not mount, or a restored
                           database without its plugin directory all land here.

    flow_name_mismatch     A `librarypluginflow` row's `plugin_name` does not
                           equal the `plugin_id` of the `plugins` row it points
                           at. `librarypluginflow.plugin_name` holds the
                           DIRECTORY id, while `enabledplugins.plugin_name`
                           holds the DISPLAY name - the two columns share a
                           name and mean different things, and this is the
                           recurring trap the issue asks to be called out.
                           Reported as a warning, not an error: execution keys
                           off the foreign key, so a mismatch here misleads a
                           human reading the table but does not by itself stop
                           a plugin running.

    WHAT IS DELIBERATELY NOT REPORTED
    ---------------------------------
    "Enabled but with no `librarypluginflow` position" - which the issue asks
    for - is NOT reported, because it is the normal, healthy state of almost
    every plugin. `Library.set_enabled_plugins` writes flow rows only for
    plugins whose info.json declares a non-zero `priorities` entry; every other
    plugin is enabled with no flow row at all and runs perfectly well on the
    default ordering. A check that fired here would light up on a correct
    install, and a check that cries wolf gets switched off and then protects
    nothing. Flow rows are the optional half of the pair, so only the
    flow-without-enablement direction is a genuine inconsistency.

    Also not reported: a drift between `enabledplugins.plugin_name` and the
    plugin's current display name. That column is written once from
    `Plugins.name` and nothing reads it back, so it goes stale legitimately
    every time a plugin update renames itself.

    Also not reported: an installed plugin that is missing from disk but is not
    enabled or ordered anywhere. It cannot fail to run, because nothing asked
    it to.

    WHY THIS DOES NOT REPAIR ANYTHING
    ---------------------------------
    Every finding here has more than one correct resolution. A
    flow_without_enabled row means either "I meant to enable this plugin and
    forgot" or "I disabled this plugin and the stale ordering is harmless" -
    and the two repairs are opposites. Rewriting a user's plugin configuration
    to whichever guess is tidier is the same class of mistake as the silence
    this module exists to fix: the system would have quietly decided something
    on the operator's behalf and told them nothing useful. So it reports
    precisely, names the library and the plugin directory id, and stops.

    WHEN IT RUNS
    ------------
    Once, at startup, before any worker exists. That is on purpose. Plugin
    install and uninstall both pass through states that look inconsistent from
    the outside - `PluginsHandler.uninstall_plugins_by_db_table_id` removes
    info.json, then the directory, then the `enabledplugins` rows, then the
    `plugins` row - so a validator running continuously would fire on healthy
    work in progress. At startup no install is in flight.

    The cost of that choice is honest and worth stating: an inconsistency
    introduced while the service is running is not noticed until the next
    restart, and the notification raised at startup is not withdrawn
    automatically once the operator fixes the problem. Dismissing it is a
    click; re-running it mid-flight would mean false alarms.

    MERGED IN: DECLARED PYTHON DEPENDENCIES (#39)
    ---------------------------------------------
    `trawlarr.libs.plugin_dependencies.validate_installed_dependencies` uses
    the seam below: it returns `RegistrationFinding`s of its own for a plugin
    whose `info.json` declares Python dependencies that are not installed,
    are out of date, or were installed for a different Python version. It is
    handed only the plugins referenced by `enabledplugins`/
    `librarypluginflow`, so it stays as quiet as the rest of this module on a
    healthy install - and a plugin that declares no dependencies, which is
    every plugin today, can never produce a finding from it.

    SEAM FOR #40
    ------------
    Issue #40 (required plugin settings failing loudly) is a different question
    about the same startup moment - it inspects plugin SETTINGS rather than
    table registration. It should add its own validator returning
    `RegistrationFinding`-shaped results and have the startup entry point merge
    the two reports, rather than growing new finding codes in here. The finding
    type and `RegistrationReport.extend` exist for exactly that; nothing else
    in this module is settings-aware.
"""

import os

from trawlarr.libs.logs import TrawlarrLogging

#: Finding codes. See the module docstring for what each one means.
FINDING_FLOW_WITHOUT_ENABLED = 'flow_without_enabled'
FINDING_MISSING_PLUGIN_RECORD = 'missing_plugin_record'
FINDING_PLUGIN_NOT_INSTALLED = 'plugin_not_installed'
FINDING_FLOW_NAME_MISMATCH = 'flow_name_mismatch'

SEVERITY_ERROR = 'error'
SEVERITY_WARNING = 'warning'

#: The UUID of the UI notification. Stable so a restart replaces the previous
#: notification in place rather than stacking a second copy of it.
NOTIFICATION_UUID = 'pluginRegistrationInconsistent'

#: How many individual findings to name in the notification body. The full
#: list always goes to the log; the notification is a pointer, not a report.
NOTIFICATION_DETAIL_LIMIT = 5

logger = TrawlarrLogging.get_logger(name='PluginRegistration')


class RegistrationFinding(object):
    """
    One inconsistency, phrased so that a human can act on it.

    :ivar code:          one of the FINDING_* constants
    :ivar severity:      SEVERITY_ERROR (a plugin will not run, or a row is
                         dead) or SEVERITY_WARNING (misleading, not fatal)
    :ivar message:       the whole finding as one human-readable sentence
    :ivar library_id:    the library affected, or None if the finding spans
                         several libraries
    :ivar library_name:  best-effort display name for `library_id`
    :ivar plugin_id:     the plugin DIRECTORY id, which is what the operator
                         needs in order to find the plugin. None when the row
                         is so broken that the directory id is unknown.
    :ivar plugin_row_id: the `plugins` table primary key the row referenced
    """

    def __init__(self, code, severity, message, library_id=None, library_name=None, plugin_id=None,
                 plugin_row_id=None):
        self.code = code
        self.severity = severity
        self.message = message
        self.library_id = library_id
        self.library_name = library_name
        self.plugin_id = plugin_id
        self.plugin_row_id = plugin_row_id

    @property
    def sort_key(self):
        return (self.code, self.library_id or 0, self.plugin_id or '', self.plugin_row_id or 0)

    def as_dict(self):
        return {
            'code':          self.code,
            'severity':      self.severity,
            'message':       self.message,
            'library_id':    self.library_id,
            'library_name':  self.library_name,
            'plugin_id':     self.plugin_id,
            'plugin_row_id': self.plugin_row_id,
        }

    def __repr__(self):
        return '<RegistrationFinding {} {}>'.format(self.code, self.message)


class RegistrationReport(object):
    """
    The findings from one validation pass.
    """

    def __init__(self, findings=None):
        self.findings = list(findings or [])

    @property
    def ok(self):
        return not self.findings

    @property
    def errors(self):
        return [f for f in self.findings if f.severity == SEVERITY_ERROR]

    @property
    def warnings(self):
        return [f for f in self.findings if f.severity == SEVERITY_WARNING]

    @property
    def severity(self):
        """
        The severity the whole report should be raised at.
        """
        return SEVERITY_ERROR if self.errors else SEVERITY_WARNING

    def add(self, finding):
        self.findings.append(finding)

    def extend(self, findings):
        """
        Merge in findings produced elsewhere. The seam #40 should use.

        :param findings: an iterable of RegistrationFinding, or another report
        :return:
        """
        if isinstance(findings, RegistrationReport):
            findings = findings.findings
        self.findings.extend(findings)

    def sorted_findings(self):
        """
        Deterministic order, so two runs over the same database produce the
        same log and the same notification text.

        :return:
        """
        return sorted(self.findings, key=lambda f: f.sort_key)

    def summary(self):
        """
        A one-line headline for the notification.

        :return:
        """
        if self.ok:
            return "Plugin registration is consistent"
        parts = []
        if self.errors:
            parts.append("{} problem{}".format(len(self.errors), '' if len(self.errors) == 1 else 's'))
        if self.warnings:
            parts.append("{} warning{}".format(len(self.warnings), '' if len(self.warnings) == 1 else 's'))
        return "Plugin registration check found {}".format(" and ".join(parts))

    def notification_message(self):
        """
        The notification body: the headline, then the first few findings
        verbatim, then a pointer to the log for the rest.

        Findings are quoted rather than summarised because the whole failure
        mode this addresses is a message that was too vague to act on.

        :return:
        """
        findings = self.sorted_findings()
        lines = [self.summary() + '.']
        for finding in findings[:NOTIFICATION_DETAIL_LIMIT]:
            lines.append(finding.message)
        remaining = len(findings) - NOTIFICATION_DETAIL_LIMIT
        if remaining > 0:
            lines.append("...and {} more. See the Trawlarr log for the full list.".format(remaining))
        lines.append("Trawlarr has not changed anything - review your library plugin configuration.")
        return " ".join(lines)


def _plugin_directory_is_installed(plugins_directory, plugin_directory_id):
    """
    Is this plugin actually on disk, in the form the executor needs?

    `PluginExecutor` imports `<plugins_directory>/<plugin_id>/plugin.py` and
    silently yields no runner when that import fails, so plugin.py is the
    honest test of "will this plugin fire". Note that an uninstall deletes
    info.json first and the whole directory immediately after, so the narrow
    window where info.json is gone but plugin.py remains is not separately
    detected - it closes on its own.

    Returns a reason string when the plugin is missing, or None when it is
    present.

    :param plugins_directory:
    :param plugin_directory_id:
    :return:
    """
    if not plugins_directory or not plugin_directory_id:
        return None
    plugin_path = os.path.join(plugins_directory, plugin_directory_id)
    # NOTE: deliberately not PluginsHandler.get_plugin_path(), which CREATES
    # the directory as a side effect. A diagnostic must not alter what it is
    # inspecting.
    if not os.path.isdir(plugin_path):
        return "no plugin directory at '{}'".format(plugin_path)
    if not os.path.isfile(os.path.join(plugin_path, 'plugin.py')):
        return "the directory '{}' contains no plugin.py".format(plugin_path)
    return None


def _default_plugins_directory():
    try:
        from trawlarr import config
        return config.Config().get_plugins_path()
    except Exception:
        logger.debug("Unable to resolve the plugins directory", exc_info=True)
        return None


def _describe_library(library_id, library_names):
    name = library_names.get(library_id)
    if name:
        return "library '{}' (id {})".format(name, library_id)
    return "library id {}".format(library_id)


def validate_plugin_registration(plugins_directory=None):
    """
    Read the three plugin tables and report where they disagree.

    Reads only. Nothing here writes, deletes or creates - not a row, not a
    directory.

    The tables are pulled whole into dictionaries rather than joined, for two
    reasons: they are tiny, and a row whose foreign key does not resolve is
    exactly what we are hunting, so it must not be dropped by a join.

    :param plugins_directory: overrides the configured plugins path
    :return: RegistrationReport
    """
    from trawlarr.libs.unmodels import EnabledPlugins, Libraries, LibraryPluginFlow, Plugins

    report = RegistrationReport()

    if plugins_directory is None:
        plugins_directory = _default_plugins_directory()

    plugins_by_row_id = {row['id']: row for row in Plugins.select().dicts()}
    library_names = {row['id']: row.get('name') for row in Libraries.select().dicts()}
    flow_rows = list(LibraryPluginFlow.select().dicts())
    enabled_rows = list(EnabledPlugins.select().dicts())

    # A fresh install has no plugins, no flow and no enablement. Three empty
    # tables agree with each other perfectly; say nothing.
    if not flow_rows and not enabled_rows:
        return report

    enabled_keys = {(row.get('library_id'), row.get('plugin_id')) for row in enabled_rows}

    # --- librarypluginflow -------------------------------------------------
    for row in flow_rows:
        library_id = row.get('library_id')
        plugin_row_id = row.get('plugin_id')
        plugin = plugins_by_row_id.get(plugin_row_id)

        if plugin is None:
            report.add(RegistrationFinding(
                code=FINDING_MISSING_PLUGIN_RECORD,
                severity=SEVERITY_ERROR,
                library_id=library_id,
                library_name=library_names.get(library_id),
                plugin_id=row.get('plugin_name'),
                plugin_row_id=plugin_row_id,
                message=(
                    "Plugin flow position for {} references plugin record {}, which is not in the plugins "
                    "table (recorded plugin_name '{}'). The row does nothing and no plugin runs from it."
                ).format(_describe_library(library_id, library_names), plugin_row_id, row.get('plugin_name')),
            ))
            continue

        plugin_directory_id = plugin.get('plugin_id')

        if (library_id, plugin_row_id) not in enabled_keys:
            report.add(RegistrationFinding(
                code=FINDING_FLOW_WITHOUT_ENABLED,
                severity=SEVERITY_ERROR,
                library_id=library_id,
                library_name=library_names.get(library_id),
                plugin_id=plugin_directory_id,
                plugin_row_id=plugin_row_id,
                message=(
                    "Plugin '{}' has a '{}' flow position on {} but is not enabled for it. Enablement is what "
                    "gates execution, so this plugin does not run against that library; the flow position is "
                    "stale."
                ).format(plugin_directory_id, row.get('plugin_type'), _describe_library(library_id, library_names)),
            ))

        if row.get('plugin_name') != plugin_directory_id:
            report.add(RegistrationFinding(
                code=FINDING_FLOW_NAME_MISMATCH,
                severity=SEVERITY_WARNING,
                library_id=library_id,
                library_name=library_names.get(library_id),
                plugin_id=plugin_directory_id,
                plugin_row_id=plugin_row_id,
                message=(
                    "Plugin flow row on {} records plugin_name '{}' but points at the plugin whose directory id "
                    "is '{}'. librarypluginflow.plugin_name holds the plugin DIRECTORY id, not the display name "
                    "(enabledplugins.plugin_name holds the display name). Execution follows the foreign key, so "
                    "this misleads a human reading the table rather than breaking the plugin."
                ).format(_describe_library(library_id, library_names), row.get('plugin_name'), plugin_directory_id),
            ))

    # --- enabledplugins ----------------------------------------------------
    for row in enabled_rows:
        library_id = row.get('library_id')
        plugin_row_id = row.get('plugin_id')
        if plugin_row_id in plugins_by_row_id:
            continue
        report.add(RegistrationFinding(
            code=FINDING_MISSING_PLUGIN_RECORD,
            severity=SEVERITY_ERROR,
            library_id=library_id,
            library_name=library_names.get(library_id),
            plugin_id=None,
            plugin_row_id=plugin_row_id,
            message=(
                "{} has plugin record {} enabled (recorded as '{}'), but that record is not in the plugins "
                "table. Nothing can run from it."
            ).format(
                _describe_library(library_id, library_names).capitalize(),
                plugin_row_id,
                row.get('plugin_name'),
            ),
        ))

    # --- on disk -----------------------------------------------------------
    # Only plugins something actually asked to run. A plugin missing from disk
    # that no library enabled cannot fail to fire.
    referenced = {}
    for row in enabled_rows + flow_rows:
        plugin = plugins_by_row_id.get(row.get('plugin_id'))
        if plugin is None:
            continue
        referenced.setdefault(plugin.get('plugin_id'), set()).add(row.get('library_id'))

    for plugin_directory_id in sorted(referenced):
        reason = _plugin_directory_is_installed(plugins_directory, plugin_directory_id)
        if not reason:
            continue
        libraries = sorted(lib_id for lib_id in referenced[plugin_directory_id] if lib_id is not None)
        report.add(RegistrationFinding(
            code=FINDING_PLUGIN_NOT_INSTALLED,
            severity=SEVERITY_ERROR,
            library_id=libraries[0] if len(libraries) == 1 else None,
            library_name=library_names.get(libraries[0]) if len(libraries) == 1 else None,
            plugin_id=plugin_directory_id,
            message=(
                "Plugin '{}' is registered against {} but is not installed: {}. Trawlarr skips a plugin it "
                "cannot load without reporting it, so this plugin silently does not run."
            ).format(
                plugin_directory_id,
                ", ".join(_describe_library(lib_id, library_names) for lib_id in libraries) or 'no library',
                reason,
            ),
        ))

    # --- declared Python dependencies --------------------------------------
    # Issue #39's validator, merged in through the seam described above. It
    # owns its own finding codes and its own explanations; all this end needs
    # to know is which plugins something asked to run.
    try:
        from trawlarr.libs import plugin_dependencies
        report.extend(plugin_dependencies.validate_installed_dependencies(plugins_directory, referenced.keys()))
    except Exception:
        logger.exception("Plugin dependency check failed to run")

    return report


def report_plugin_registration(plugins_directory=None, raise_notification=True):
    """
    Run the validation and make the result impossible to miss: every finding
    to the log at its own severity, and one notification in the UI.

    Never raises. A diagnostic that can take the service down is worse than the
    silence it was written to replace, so any failure in here is logged and
    swallowed.

    :param plugins_directory:
    :param raise_notification: set False to log only
    :return: RegistrationReport, or None if the check itself could not run
    """
    try:
        report = validate_plugin_registration(plugins_directory=plugins_directory)
    except Exception:
        logger.exception("Plugin registration consistency check failed to run")
        return None

    try:
        if report.ok:
            logger.info("Plugin registration is consistent across the plugins, enabledplugins and "
                        "librarypluginflow tables")
            return report

        logger.warning("%s. Trawlarr has NOT modified your configuration.", report.summary())
        for finding in report.sorted_findings():
            if finding.severity == SEVERITY_ERROR:
                logger.error("[%s] %s", finding.code, finding.message)
            else:
                logger.warning("[%s] %s", finding.code, finding.message)

        if raise_notification:
            from trawlarr.libs.notifications import Notifications
            notifications = Notifications()
            notifications.update({
                'uuid':       NOTIFICATION_UUID,
                'type':       report.severity,
                'icon':       'report_problem',
                'label':      'pluginRegistrationLabel',
                'message':    report.notification_message(),
                'navigation': {
                    'push': '/ui/settings-library',
                },
            })
    except Exception:
        logger.exception("Failed to report the plugin registration consistency check result")

    return report
