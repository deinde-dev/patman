# SPDX-License-Identifier: GPL-2.0+
"""Set up pytest for patman's tests"""

import os

from patman import lore
from patman import test_common

# As 'patman test' does: keep the tests away from the user's patchwork cache,
# and lore
test_common.clear_git_env()
os.environ['PATMAN_PWCACHE'] = 'off'
lore.set_mode('off')

# The demo scripts need Playwright, which the tests do not, so do not
# import them for doctests
collect_ignore = ['doc/demo']
