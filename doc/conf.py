# SPDX-License-Identifier: GPL-2.0+
"""Sphinx configuration for the patman documentation."""

import pathlib
import sys
import tomllib

_pyproject = pathlib.Path(__file__).resolve().parent.parent / 'pyproject.toml'
with open(_pyproject, 'rb') as _fd:
    _meta = tomllib.load(_fd)['project']

project = 'Patman'
author = 'Simon Glass and contributors'
copyright = '2011-2026, Simon Glass and contributors'
release = _meta['version']
version = release

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / '_ext'))
extensions = ['patman_cmdref']
templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

html_theme = 'sphinx_rtd_theme'
html_static_path = []
html_logo = '../logo/patman-logo-primary.svg'
html_favicon = '../logo/patman-icon.svg'

# Keep the table of contents fully expanded in the sidebar, so clicking
# a section does not collapse the rest of the navigation.
html_theme_options = {
    'collapse_navigation': False,
    'sticky_navigation': True,
    'navigation_depth': 4,
}
