#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.setup.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     04 May 2020, (10:47 AM)
    Modified 2026 by Ryan Gregg as part of Trawlarr.

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

    ---

    Static packaging metadata now lives in pyproject.toml (issue #58). What
    is left here is what PEP 621 has no way to express:

      * `version`, which is derived from `git describe` by versioninfo.py;
      * the custom build commands -- `build_py` is overridden so that every
        wheel and sdist runs an `npm ci` + Quasar production build and drops
        the result in trawlarr/webserver/public. Without that the wheel
        installs a webserver with no frontend to serve.

    Nothing here declares the distribution name, the packages or the console
    scripts any more; those are in pyproject.toml and pinned by
    tests/unit/test_packaging_metadata.py.
"""
import json
import os
import shutil
import subprocess
import sys
import glob
from setuptools import setup, Command
import setuptools.command.build_py

if sys.version_info[0] < 3:
    print("This module version requires Python 3.")
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import versioninfo

src_dir = 'trawlarr'

module_name = versioninfo.name()


class BuildPyCommand(setuptools.command.build_py.build_py):
    """Custom build command."""

    def run(self):
        setuptools.command.build_py.build_py.run(self)
        self.run_command('write-build-version')
        self.run_command('build-frontend')


class WriteVersionCommand(Command):
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    @staticmethod
    def run():
        version_file = os.path.join('.', 'build', 'lib', src_dir, 'version')
        data = {
            'short': versioninfo.version(),
            'long':  versioninfo.full_version(),
        }
        with open(version_file, 'w') as f:
            json.dump(data, f)


class BuildFrontendCommand(setuptools.command.build_py.build_py):
    """Frontend build command."""

    def run(self):
        setuptools.command.build_py.build_py.run(self)

        public_asset_path = os.path.abspath(os.path.join('.', 'build', 'lib', src_dir, 'webserver', 'public'))
        frontend_path = os.path.abspath(os.path.join('.', 'build', 'lib', src_dir, 'webserver', 'frontend'))

        # Start by clearing out anything if this was pulled from a dirty tree
        shutil.rmtree(public_asset_path, ignore_errors=True)
        shutil.rmtree(os.path.join(frontend_path, 'node_modules'), ignore_errors=True)

        # Install all modules
        subprocess.run(
            "npm ci",
            check=True,
            shell=True,
            cwd=frontend_path,
        )
        # Build the frontend
        subprocess.run(
            "npm run build:publish",
            check=True,
            shell=True,
            cwd=frontend_path,
        )

        # Move built dist to templates directory
        shutil.move(os.path.join(frontend_path, 'dist', 'spa'), public_asset_path)
        # Remove the frontend source from the package (we will not distribute these)
        shutil.rmtree(frontend_path, ignore_errors=True)


class CleanCommand(Command):
    """Custom clean command to tidy up the project root."""
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    @staticmethod
    def run():
        shutil.rmtree(os.path.abspath(os.path.join(os.path.dirname(__file__), 'build')), ignore_errors=True)
        shutil.rmtree(os.path.abspath(os.path.join(os.path.dirname(__file__), 'dist')), ignore_errors=True)
        shutil.rmtree(os.path.abspath(os.path.join(os.path.dirname(__file__), '*.pyc')), ignore_errors=True)
        # Both names: the distribution was renamed unmanic -> trawlarr in
        # issue #49 step 4, and a checkout that has been built at least once
        # under the old name still has the stale egg-info directory sitting
        # in the project root.
        for egg_info in ('{}.egg-info'.format(module_name), 'unmanic.egg-info'):
            shutil.rmtree(os.path.abspath(os.path.join(os.path.dirname(__file__), egg_info)), ignore_errors=True)
        [shutil.rmtree(f) for f in glob.glob(src_dir + "/**/__pycache__", recursive=True)]


class FullVersionCommand(Command):
    """Print the long form of the version string."""
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    @staticmethod
    def run():
        print(versioninfo.full_version())


cmd_class = {
    'build_py':            BuildPyCommand,
    'write-build-version': WriteVersionCommand,
    'build-frontend':      BuildFrontendCommand,
    'clean':               CleanCommand,
    'fullversion':         FullVersionCommand,
}

setup(
    # Declared `dynamic` in pyproject.toml because it comes from git, not
    # from a file that can be read at rest.
    version=versioninfo.version(),
    cmdclass=cmd_class,
)
