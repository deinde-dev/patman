# SPDX-License-Identifier: GPL-2.0+
"""Set up pytest for patman's tests"""

from patman import test_common

test_common.clear_git_env()

# The demo scripts need Playwright, which the tests do not, so do not
# import them for doctests
collect_ignore = ['doc/demo']
