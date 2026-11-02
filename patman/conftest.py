# SPDX-License-Identifier: GPL-2.0+
"""Set up pytest for patman's tests

This is in the package, so that it applies wherever the tests are run from,
e.g. an sdist or an installed package, as well as a checkout
"""

from patman import test_common

# As 'patman test' does: keep the tests away from the user's settings,
# caches and network
test_common.isolate()
