# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Run Coverity static analysis on a series and find new defects

Coverity works on a whole build rather than a diff, so to find the
defects a series introduces we analyse the base tree and the patched
tree separately and compare. Each Coverity defect carries a stable
'mergeKey' that identifies the same defect across builds, so the new
defects are those whose mergeKey is not present in the base.

The cov-build, cov-analyze and cov-format-errors tools must be on the
PATH; they are part of a (commercial) Coverity installation.
"""

import json
import os
import shutil
import subprocess


# Default board config to build for analysis (host build, no toolchain)
DEFAULT_DEFCONFIG = 'sandbox_defconfig'


def check_available():
    """Check whether the Coverity command-line tools are installed

    Returns:
        bool: True if cov-build, cov-analyze and cov-format-errors are all
            on the PATH
    """
    return all(shutil.which(tool) for tool in
               ('cov-build', 'cov-analyze', 'cov-format-errors'))


def _run(cmd, cwd):
    """Run a command, raising on failure

    Args:
        cmd (list of str): Command and arguments
        cwd (str): Directory to run in

    Raises:
        subprocess.CalledProcessError: if the command fails
    """
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, text=True)


def analyze(repo_path, defconfig, emit_dir):
    """Build and analyse a tree, returning its Coverity defects

    Configures the given defconfig, builds it under cov-build, runs
    cov-analyze and reads the defects back as JSON.

    Args:
        repo_path (str): Worktree to configure and build in
        defconfig (str): Board defconfig to build, e.g. 'sandbox_defconfig'
        emit_dir (str): Coverity intermediate directory to write

    Returns:
        list of dict: Defect records, each with at least 'mergeKey'

    Raises:
        subprocess.CalledProcessError: if a build or analysis step fails
    """
    jobs = str(os.cpu_count() or 1)
    _run(['make', defconfig], repo_path)
    _run(['cov-build', '--dir', emit_dir, 'make', '-j', jobs], repo_path)
    _run(['cov-analyze', '--dir', emit_dir], repo_path)
    out_json = os.path.join(emit_dir, 'defects.json')
    _run(['cov-format-errors', '--dir', emit_dir, '--json-output-v7',
          out_json], repo_path)
    with open(out_json, encoding='utf-8') as fd:
        data = json.load(fd)
    return data.get('issues', [])


def find_new_defects(base, patched):
    """Find defects present in the patched tree but not the base

    Args:
        base (list of dict): Defects from the base tree
        patched (list of dict): Defects from the patched tree

    Returns:
        list of dict: Defects whose mergeKey is not in the base
    """
    base_keys = {defect.get('mergeKey') for defect in base}
    return [defect for defect in patched
            if defect.get('mergeKey') not in base_keys]


def format_defect(defect):
    """Format a single defect as a one-line summary

    Args:
        defect (dict): Coverity defect record

    Returns:
        str: Summary like 'RESOURCE_LEAK: drivers/foo.c:42 (probe): ...'
    """
    checker = defect.get('checkerName', '?')
    path = defect.get('mainEventFilePathname', '?')
    line = defect.get('mainEventLineNumber', '?')
    func = defect.get('functionDisplayName', '')
    desc = (defect.get('subcategoryLongDescription') or
            defect.get('subcategoryShortDescription') or '')
    loc = f'{path}:{line}'
    if func:
        loc += f' ({func})'
    return f'{checker}: {loc}: {desc}'.rstrip(': ')


def format_defects(defects):
    """Format a list of defects as a bullet list, most useful first

    Args:
        defects (list of dict): Defects to format

    Returns:
        str: One '- <summary>' line per defect, or '' if there are none
    """
    return '\n'.join(f'- {format_defect(defect)}' for defect in defects)
