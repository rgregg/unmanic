#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.libs.plugin_dependencies

    Declared Python dependencies for plugins (issue #39).

    THE PROBLEM
    -----------
    A plugin that needs a Python package the image does not ship had two
    options, and both fail while looking like success:

      * `pip install` into the container's venv by hand. It works until the
        image is replaced, and it is never done on the next worker. The
        plugin then raises ImportError at first execution - i.e. in the
        middle of processing a file, not when the operator enabled it.

      * Vendor the package into the plugin and append `site-packages/` to
        `sys.path` from inside plugin.py. That imports from a directory the
        plugin's own repository does not track, so it runs in the container
        it was hand-built in and breaks from a clean checkout.

    WHAT THIS ADDS
    --------------
    A plugin may declare, in its `info.json`::

        "python_dependencies": ["requests>=2.31", "pillow==10.3.0"]

    Those requirements are installed when the plugin is installed or
    updated, and a receipt is written next to them. If the install cannot be
    done, the install FAILS - the plugin is not written to the plugins table
    and does not appear installed, let alone enabled.

    WHERE THEY ARE INSTALLED, AND WHY
    ---------------------------------
    Plugin-scoped: `<plugins_path>/<plugin_id>/site-packages`, which
    `PluginExecutor.__include_plugin_site_packages` already puts on sys.path
    before importing the plugin. Three reasons over the shared venv:

      1. The plugins path lives on the operator's persistent config volume.
         The venv lives inside the container image, may be read-only, and is
         replaced wholesale on every image update. Installing into the venv
         is the exact failure the issue is about; doing it automatically
         would only make it happen faster.
      2. It is the directory the field-issue plugin was already hand-rolling.
         This replaces an untracked hack with a tracked, receipted one.
      3. Two plugins pinning incompatible versions of the same package do not
         fight over one site-packages.

    The cost is honest: a package needed by three plugins is on disk three
    times, and `sys.path` ordering means the first plugin loaded wins if two
    plugins import different versions of the same module name in one process.
    Plugin-scoped directories bound the damage; they do not abolish it.

    SURVIVING A RESTART
    -------------------
    The receipt (`site-packages/.trawlarr-plugin-deps.json`) records the
    requirement strings that were installed and the `major.minor` of the
    interpreter that installed them. On startup the enabled plugins are
    checked against their receipts, and a mismatch is reported loudly:

      * no receipt, or a receipt that does not cover the currently declared
        requirements - the plugin was installed before it declared them, or
        the site-packages directory did not come across with the volume;
      * a receipt written by a different Python `major.minor` - the image
        was updated across a Python version bump, and any compiled extension
        module in there will not import.

    Nothing is re-installed automatically. Re-installing at startup would
    mean this service reaches out to a package index and executes whatever
    it finds, unattended, on every boot - a much worse thing to own than a
    loud message. The operator re-installs the plugin, which is one click
    and already runs the whole path.

    TRUST, EXPLICITLY
    -----------------
    Installing a declared requirement means running `pip`, which downloads
    and executes third-party code named by third-party plugin metadata
    fetched over the network. That is strictly more trust than Trawlarr
    asked for before, so it is OPT-IN: without
    `TRAWLARR_ALLOW_PLUGIN_DEPENDENCY_INSTALL=true`, a plugin that declares
    dependencies is REFUSED at install time with a message naming the
    packages it wanted. Refused, not silently installed-without-deps: a
    plugin whose imports will fail must not look installed.

    Requirement strings are also constrained to plain
    `name[extras][version-specifiers]` (see `validate_requirement`). A
    requirement is not allowed to be a URL, a local path, an editable
    install, or a pip option - so plugin metadata cannot redirect pip at an
    arbitrary index, or point it at a file inside the container. It still
    names packages on the configured index, and that is the trust the
    operator opts into.

    THE OTHER ROUTES INTO A PACKAGE MANAGER (issue #88)
    ---------------------------------------------------
    Declared dependencies were never the only way plugin metadata could
    start a package install. Since long before this module existed:

      * `requirements.post-install.txt` in the plugin zip runs
        `pip install -r` on it, unconditionally;
      * `requirements.txt` plus `"defer_dependency_install": true` in
        info.json does the same, and additionally runs `npm install` and
        `npm run build` if the plugin ships a `package.json` - which
        executes that package.json's lifecycle scripts.

    Neither was behind `ALLOW_INSTALL_ENV_VAR`, so the gate an operator
    reads about could be off while a freshly installed plugin still drove
    pip. Two rules, one of them invisible.

    These paths are now behind the SAME gate as declared dependencies (see
    `assert_requirements_file_install_permitted` and
    `assert_npm_install_permitted`). That is a behaviour change, and the
    honest measure of its cost is what it breaks in the wild: of the 56
    plugins in the official catalog, zero ship a
    `requirements.post-install.txt` and zero set `defer_dependency_install`.
    54 of them ship a `requirements.txt`, but without that flag it is
    build-time metadata for the plugin's own CI - those plugins vendor the
    resulting `site-packages/` into their zip, and Trawlarr never reads
    their requirements file at all. So the previous claim here, that gating
    "would break every existing plugin that ships one", was simply wrong: it
    counted files, not installs.

    The grammar restriction stays, and is enforced even with the gate ON: a
    requirements file is plugin metadata like any other, so a pip option
    (`--index-url` above all), a URL, a VCS reference or a local path in
    that file refuses the whole plugin install. Belt and braces - the gate
    decides whether pip runs, the grammar decides what it may be told.

    npm gets the gate but no grammar check. A package.json is a program,
    not a list of names; there is no subset of it worth pretending to
    validate. Off by default is the whole of the protection there, and
    docs/PLUGIN-DEPENDENCIES.md says so.

    WHAT THIS DOES NOT DO
    ---------------------
    No lockfile, no hash pinning, no dependency resolution across plugins,

    no uninstall of a removed dependency (the site-packages directory is
    rebuilt from scratch on each install, so a dropped requirement
    disappears with it). No check that an installed package still imports -
    the receipt records what pip was asked for and that pip succeeded, not
    that the result works.

    A plugin that declares nothing - which is every plugin in existence
    today - takes no new code path anywhere in here. `read_declared_dependencies`
    returns an empty list, and every entry point short-circuits on that.
"""

import json
import os
import re
import shutil
import subprocess
import sys

from trawlarr.libs import envvars
from trawlarr.libs.logs import TrawlarrLogging

logger = TrawlarrLogging.get_logger(name='PluginDependencies')

#: The `info.json` key a plugin uses to declare its Python requirements.
INFO_JSON_KEY = 'python_dependencies'

#: Environment variable gating dependency installation. Absent or false, a
#: plugin that declares dependencies is refused rather than installed.
ALLOW_INSTALL_ENV_VAR = envvars.ALLOW_PLUGIN_DEPENDENCY_INSTALL_ENV_VAR

#: Directory, relative to the plugin directory, that dependencies go into.
#: PluginExecutor already adds this to sys.path.
SITE_PACKAGES_DIRNAME = 'site-packages'

#: Receipt of what was installed, written inside SITE_PACKAGES_DIRNAME.
RECEIPT_FILENAME = '.trawlarr-plugin-deps.json'

#: Seconds before a pip invocation is abandoned. A wedged pip must not hold
#: the install request open forever.
INSTALL_TIMEOUT = 600

#: A requirement we are willing to hand to pip: a PEP 508 name, optional
#: extras, optional version specifiers. Deliberately narrow - anything with
#: a URL, a marker, a path, or a leading dash is rejected rather than
#: sanitised, because a requirement string is not a place to be clever.
_REQUIREMENT_RE = re.compile(
    r'^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?'          # distribution name
    r'(\[[A-Za-z0-9._,-]+\])?'                            # optional extras
    r'((===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]*'
    r'(\s*,\s*(===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]*)*)?$'
)

#: Findings produced by `validate_installed_dependencies`. Shaped to merge
#: into plugin_registration.RegistrationReport - see that module's docstring.
FINDING_DEPENDENCIES_NOT_INSTALLED = 'plugin_dependencies_not_installed'
FINDING_DEPENDENCIES_STALE = 'plugin_dependencies_stale'
FINDING_DEPENDENCIES_WRONG_PYTHON = 'plugin_dependencies_wrong_python'


class PluginDependencyError(Exception):
    """
    A declared dependency could not be honoured.

    Raised out of the install path on purpose: `install_plugin` is called
    inside a try/except that abandons the install, so raising here is what
    stops a plugin with unsatisfiable dependencies from being recorded as
    installed.
    """
    pass


def _truthy(value):
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'enable', 'enabled')


def installs_are_permitted(environ=None):
    """
    Has the operator opted in to letting plugin metadata drive pip?

    :param environ: defaults to os.environ
    :return: bool
    """
    if environ is None:
        environ = os.environ
    return _truthy(environ.get(ALLOW_INSTALL_ENV_VAR))


def validate_requirement(requirement):
    """
    Accept a plain `name[extras][specifiers]` requirement, reject the rest.

    Rejecting rather than quoting is the point. A requirement that is a URL,
    a path, an editable install or a pip option would let plugin metadata
    choose where pip fetches code from, which is a decision the operator
    made when they configured the index - not one a third-party plugin
    author gets to override.

    :param requirement:
    :return: the stripped requirement
    :raises PluginDependencyError: if it is anything else
    """
    if not isinstance(requirement, str):
        raise PluginDependencyError(
            "Plugin dependency must be a string, got {}: {!r}".format(type(requirement).__name__, requirement))
    candidate = requirement.strip()
    if not candidate:
        raise PluginDependencyError("Plugin declared an empty dependency string")
    if not _REQUIREMENT_RE.match(candidate):
        raise PluginDependencyError(
            "Refusing plugin dependency '{}'. Trawlarr installs only plain package requirements "
            "(name, optional [extras], optional version specifiers). URLs, file paths, editable "
            "installs, environment markers and pip options are not accepted from plugin metadata."
            .format(candidate))
    return candidate


def read_declared_dependencies(plugin_info):
    """
    The requirements a plugin declares in its info.json, validated.

    Returns [] for the overwhelmingly common case of a plugin that declares
    nothing, including a plugin whose key is present but an empty list.

    :param plugin_info: parsed info.json
    :return: list of requirement strings
    :raises PluginDependencyError: if the declaration is malformed or a
            requirement is not acceptable
    """
    if not plugin_info:
        return []
    declared = plugin_info.get(INFO_JSON_KEY)
    if declared is None:
        return []
    if isinstance(declared, str) or not isinstance(declared, (list, tuple)):
        raise PluginDependencyError(
            "Plugin '{}' must be a list of requirement strings, got {}".format(
                INFO_JSON_KEY, type(declared).__name__))
    return [validate_requirement(item) for item in declared]


def scan_requirements_file(requirements_file):
    """
    The lines of a plugin-shipped requirements file that must not reach pip.

    A plugin shipping `requirements.txt` or `requirements.post-install.txt`
    has caused `pip install -r` to run at install time since long before this
    module existed. That path is now behind `ALLOW_INSTALL_ENV_VAR` too
    (issue #88), but this check runs regardless of the gate: opting in to
    plugin dependency installs is not opting in to letting a plugin choose
    the index. See the boundary described in docs/PLUGIN-DEPENDENCIES.md.

    What is NOT acceptable, opt-in or not, is that a requirements FILE
    carries the full pip grammar: `--index-url` redirects pip at an index the
    operator never configured, a URL or `-e` line makes pip fetch and execute
    code from an arbitrary host, and `-r /etc/...` reads a path inside the
    container. Those are precisely the powers `validate_requirement` refuses
    to take from plugin metadata, and a requirements file is plugin metadata
    by another name. Refusing them here closes the hole without touching the
    plain `name==version` lines that every real plugin actually ships.

    Physical lines are scanned independently, including continuations, so an
    option hidden after a trailing backslash is still seen.

    :param requirements_file:
    :return: list of (line number, line, reason) for every refused line
    """
    refused = []
    try:
        with open(requirements_file, 'r', errors='replace') as handle:
            lines = handle.readlines()
    except OSError:
        logger.warning("Unable to read plugin requirements file '%s'", requirements_file, exc_info=True)
        return refused

    for number, raw in enumerate(lines, start=1):
        line = raw.split('#', 1)[0].strip()
        # A trailing backslash is a line continuation; the content still has
        # to stand on its own here.
        line = line.rstrip('\\').strip()
        if not line:
            continue
        reason = None
        if line.startswith('-'):
            reason = ("pip options are not accepted from a plugin: they can redirect pip at an index or a "
                      "host the operator did not configure, or read a file inside the container")
        elif '://' in line or line.lower().startswith(('git+', 'hg+', 'svn+', 'bzr+')):
            reason = "a URL requirement makes pip fetch and execute code from a host the operator did not choose"
        elif line.startswith(('/', '.', '~')) or (len(line) > 1 and line[1] == ':'):
            reason = "a path requirement makes pip install from a location inside the container"
        if reason is not None:
            refused.append((number, line, reason))
    return refused


def assert_requirements_file_is_safe(plugin_id, requirements_file):
    """
    Refuse a plugin whose requirements file would hand pip more than package
    names.

    Raises rather than filtering the file: silently dropping a line would
    install a plugin whose dependencies are not what its author wrote, which
    is a different way to be broken.

    :param plugin_id:
    :param requirements_file:
    :raises PluginDependencyError:
    """
    refused = scan_requirements_file(requirements_file)
    if not refused:
        return
    detail = '; '.join("line {}: '{}' ({})".format(number, line, reason) for number, line, reason in refused)
    raise PluginDependencyError(
        "Refusing to install plugin '{}': its '{}' contains {} that Trawlarr will not pass to pip. {}. "
        "Trawlarr installs plain package requirements from the index this installation is configured with, "
        "and nothing else.".format(
            plugin_id or os.path.basename(os.path.dirname(str(requirements_file))),
            os.path.basename(str(requirements_file)),
            'a line' if len(refused) == 1 else '{} lines'.format(len(refused)),
            detail))


def requirement_lines(requirements_file):
    """
    The requirement lines of a plugin-shipped requirements file.

    Comments and blank lines dropped, nothing else interpreted. Used only
    to tell the operator what the plugin wanted, so an unreadable file is
    an empty list rather than an error - the caller is already refusing.

    :param requirements_file:
    :return: list of str
    """
    try:
        with open(requirements_file, 'r', errors='replace') as handle:
            lines = handle.readlines()
    except OSError:
        logger.warning("Unable to read plugin requirements file '%s'", requirements_file, exc_info=True)
        return []
    stripped = (raw.split('#', 1)[0].strip() for raw in lines)
    return [line for line in stripped if line]


def assert_requirements_file_install_permitted(plugin_id, requirements_file, environ=None):
    """
    Refuse to run pip for a plugin-shipped requirements file unless the
    operator opted in.

    Issue #88. A `requirements.post-install.txt`, or a `requirements.txt`
    with `defer_dependency_install`, made pip run at install time with no
    gate at all - so an operator who left `ALLOW_INSTALL_ENV_VAR` off, and
    read the documentation saying plugin metadata could not drive pip,
    could still get a pip install from a plugin they installed. This puts
    that path behind the same switch as a declared dependency, so there is
    one rule instead of two.

    Raises rather than skipping the install: a plugin whose imports are
    going to fail must not end up recorded as installed. Same reasoning as
    `install_dependencies`, and it must be the same reasoning, or the two
    routes diverge again.

    :param plugin_id:
    :param requirements_file:
    :param environ: defaults to os.environ
    :raises PluginDependencyError: if installs are not permitted
    """
    if installs_are_permitted(environ=environ):
        return
    wanted = requirement_lines(requirements_file)
    raise PluginDependencyError(
        "Plugin '{}' ships a '{}' ({}) and installing plugin dependencies is disabled. Installing it "
        "runs pip against a package index using names taken from a third-party plugin's files. Set "
        "{}=true to allow it, or install these packages into the image yourself. Refusing to install "
        "the plugin - it would not work.".format(
            plugin_id or os.path.basename(os.path.dirname(str(requirements_file))),
            os.path.basename(str(requirements_file)),
            ', '.join(wanted) if wanted else 'no requirements',
            ALLOW_INSTALL_ENV_VAR))


def assert_npm_install_permitted(plugin_id, package_file, environ=None):
    """
    Refuse to run npm for a plugin-shipped package.json unless the operator
    opted in.

    Reached only through `defer_dependency_install`, alongside the pip path
    above, and strictly the more powerful of the two: `npm install` runs
    that package.json's lifecycle scripts, and its dependencies may name any
    registry, git URL or tarball. There is no `scan_requirements_file`
    equivalent here and this module does not pretend otherwise - a
    package.json is a program, not a list of names. The gate is the whole
    of the protection.

    :param plugin_id:
    :param package_file:
    :param environ: defaults to os.environ
    :raises PluginDependencyError: if installs are not permitted
    """
    if installs_are_permitted(environ=environ):
        return
    raise PluginDependencyError(
        "Plugin '{}' ships a '{}' and installing plugin dependencies is disabled. Installing it runs "
        "'npm install', which downloads packages the plugin names and executes their install scripts. "
        "Set {}=true to allow it. Refusing to install the plugin - it would not work.".format(
            plugin_id or os.path.basename(os.path.dirname(str(package_file))),
            os.path.basename(str(package_file)),
            ALLOW_INSTALL_ENV_VAR))


def site_packages_path(plugin_path):
    return os.path.join(str(plugin_path), SITE_PACKAGES_DIRNAME)


def receipt_path(plugin_path):
    return os.path.join(site_packages_path(plugin_path), RECEIPT_FILENAME)


def _python_tag():
    return '{}.{}'.format(sys.version_info[0], sys.version_info[1])


def read_receipt(plugin_path):
    """
    The receipt left by the last successful dependency install, or None.

    :param plugin_path:
    :return: dict or None
    """
    path = receipt_path(plugin_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as handle:
            receipt = json.load(handle)
    except Exception:
        logger.warning("Unreadable plugin dependency receipt at '%s'", path, exc_info=True)
        return None
    if not isinstance(receipt, dict):
        return None
    return receipt


def _write_receipt(plugin_path, dependencies):
    with open(receipt_path(plugin_path), 'w') as handle:
        json.dump({
            'dependencies': list(dependencies),
            'python':       _python_tag(),
        }, handle, indent=2, sort_keys=True)


def install_dependencies(plugin_id, plugin_path, dependencies, environ=None):
    """
    Install a plugin's declared dependencies into its own site-packages.

    Rebuilds the directory from scratch, so a requirement the plugin no
    longer declares does not linger. Writes the receipt only after pip
    reports success.

    :param plugin_id:
    :param plugin_path:
    :param dependencies: already validated requirement strings
    :param environ: defaults to os.environ, for the opt-in check
    :return: None
    :raises PluginDependencyError: refused, pip missing, pip failed or timed out
    """
    if not dependencies:
        return

    if not installs_are_permitted(environ=environ):
        raise PluginDependencyError(
            "Plugin '{}' declares Python dependencies ({}) but installing plugin dependencies is "
            "disabled. Installing them runs pip against a package index using names taken from "
            "third-party plugin metadata. Set {}=true to allow it, or install these packages into "
            "the image yourself. Refusing to install the plugin - it would not work."
            .format(plugin_id, ', '.join(dependencies), ALLOW_INSTALL_ENV_VAR))

    target = site_packages_path(plugin_path)
    # Rebuild from empty. Leaving the previous contents in place would let a
    # package that is no longer declared keep satisfying an import.
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)

    command = [
        sys.executable, '-m', 'pip', 'install', '--upgrade',
        '--no-input', '--disable-pip-version-check',
        '--target={}'.format(target),
    ]
    # `--` so a requirement can never be read as an option, belt and braces
    # over validate_requirement having already rejected anything dash-led.
    command.append('--')
    command.extend(dependencies)

    logger.info("Installing declared dependencies for plugin '%s': %s", plugin_id, ', '.join(dependencies))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except FileNotFoundError as exc:
        raise PluginDependencyError(
            "Cannot install dependencies for plugin '{}': no Python interpreter at '{}' ({}).".format(
                plugin_id, sys.executable, exc))
    except subprocess.TimeoutExpired:
        raise PluginDependencyError(
            "Timed out after {} seconds installing dependencies for plugin '{}' ({}).".format(
                INSTALL_TIMEOUT, plugin_id, ', '.join(dependencies)))

    if result.returncode != 0:
        # pip's own diagnosis is far more useful than anything we could
        # invent, so it goes in the message verbatim rather than the log.
        detail = (result.stderr or result.stdout or '').strip()
        raise PluginDependencyError(
            "Failed to install dependencies for plugin '{}' ({}). pip exited {}:\n{}".format(
                plugin_id, ', '.join(dependencies), result.returncode, detail))

    _write_receipt(plugin_path, dependencies)
    logger.info("Installed %s dependencies for plugin '%s' into '%s'", len(dependencies), plugin_id, target)


def ensure_dependencies_installed(plugin_id, plugin_path, plugin_info, environ=None):
    """
    The install-time entry point.

    A plugin that declares nothing returns immediately, having touched
    nothing on disk. Anything else either ends with the dependencies
    installed and receipted, or raises - and a raise here aborts the plugin
    install.

    :param plugin_id:
    :param plugin_path:
    :param plugin_info: parsed info.json
    :param environ:
    :return: the list of dependencies installed (possibly empty)
    """
    dependencies = read_declared_dependencies(plugin_info)
    if not dependencies:
        return []
    install_dependencies(plugin_id, plugin_path, dependencies, environ=environ)
    return dependencies


def check_dependencies(plugin_id, plugin_path, plugin_info):
    """
    Are a plugin's declared dependencies present and usable, right now?

    Returns None when there is nothing to say - which includes the plugin
    declaring no dependencies at all. Otherwise returns
    `(code, severity, message)` describing exactly one problem.

    Deliberately does NOT try to import anything: importing a plugin's
    dependencies to test them runs third-party module-level code as a side
    effect of a health check.

    :param plugin_id:
    :param plugin_path:
    :param plugin_info: parsed info.json
    :return: (code, message) or None
    """
    try:
        dependencies = read_declared_dependencies(plugin_info)
    except PluginDependencyError as exc:
        return (FINDING_DEPENDENCIES_NOT_INSTALLED,
                "Plugin '{}' declares dependencies that Trawlarr will not install: {}".format(plugin_id, exc))

    if not dependencies:
        return None

    receipt = read_receipt(plugin_path)
    if receipt is None:
        return (FINDING_DEPENDENCIES_NOT_INSTALLED,
                "Plugin '{}' declares the Python dependencies {} but none are recorded as installed "
                "(no receipt at '{}'). The plugin's imports will fail the first time it runs. "
                "Re-install the plugin to install them.".format(
                    plugin_id, ', '.join(dependencies), receipt_path(plugin_path)))

    installed = receipt.get('dependencies')
    if not isinstance(installed, list) or sorted(installed) != sorted(dependencies):
        return (FINDING_DEPENDENCIES_STALE,
                "Plugin '{}' declares the Python dependencies {} but what is installed was built for {}. "
                "Re-install the plugin to bring them into line.".format(
                    plugin_id,
                    ', '.join(dependencies),
                    ', '.join(installed) if isinstance(installed, list) and installed else 'no dependencies'))

    receipt_python = receipt.get('python')
    if receipt_python and receipt_python != _python_tag():
        return (FINDING_DEPENDENCIES_WRONG_PYTHON,
                "Plugin '{}' has its Python dependencies installed for Python {}, but Trawlarr is running "
                "Python {}. Any compiled extension module in '{}' will fail to import. Re-install the "
                "plugin.".format(
                    plugin_id, receipt_python, _python_tag(), site_packages_path(plugin_path)))

    return None


def validate_installed_dependencies(plugins_directory, plugin_directory_ids):
    """
    Startup check over the plugins something actually asked to run.

    Returns `plugin_registration.RegistrationFinding` objects so the startup
    report can merge them - see that module's "SEAM" note. Reads only.

    A plugin that is not on disk is not reported here; the registration
    validator already reports that, and saying it twice in one notification
    helps nobody.

    :param plugins_directory:
    :param plugin_directory_ids: iterable of plugin directory ids to check
    :return: list of RegistrationFinding
    """
    from trawlarr.libs.plugin_registration import RegistrationFinding, SEVERITY_ERROR

    findings = []
    if not plugins_directory:
        return findings

    for plugin_id in sorted(set(plugin_directory_ids or [])):
        plugin_path = os.path.join(plugins_directory, plugin_id)
        info_file = os.path.join(plugin_path, 'info.json')
        if not os.path.isfile(info_file):
            continue
        try:
            with open(info_file) as handle:
                plugin_info = json.load(handle)
        except Exception:
            logger.debug("Unable to read info.json for plugin '%s'", plugin_id, exc_info=True)
            continue

        result = check_dependencies(plugin_id, plugin_path, plugin_info)
        if result is None:
            continue
        code, message = result
        findings.append(RegistrationFinding(
            code=code,
            severity=SEVERITY_ERROR,
            plugin_id=plugin_id,
            message=message,
        ))

    return findings
