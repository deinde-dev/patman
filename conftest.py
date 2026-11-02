# SPDX-License-Identifier: GPL-2.0+
"""Set up pytest for a checkout of patman

The tests are set up by patman/conftest.py
"""

# The demo scripts need Playwright, which the tests do not, so do not
# import them for doctests
collect_ignore = ['doc/demo']
