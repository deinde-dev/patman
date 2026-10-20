#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0+
#
# Copyright (c) 2011 The Chromium OS Authors.
#

"""See README for more information"""

import os
import sys

# Allow 'from patman import xxx' to work, preferring this copy of patman, e.g.
# a checkout, to one which is installed
# pylint: disable=C0413
our_path = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(our_path, '..'))

# Our modules
from patman import cmdline
from patman import control
from patman import lore
from u_boot_pylib import test_util
from u_boot_pylib import tout


def run_patman():
    """Run patamn

    This is the main program. It collects arguments and runs either the tests or
    the control module.
    """
    args = cmdline.parse_args()

    if not args.debug:
        sys.tracebacklimit = 0

    tout.init(tout.INFO if args.verbose else tout.NOTICE)

    # Run our reasonably good tests
    if args.cmd == 'test':
        # pylint: disable=C0415
        from patman import func_test
        from patman import test_checkpatch
        from patman import test_common
        from patman import test_cseries
        from patman import test_pwcache
        from patman import test_database
        from patman import test_lore
        from patman import test_review
        from patman import test_send
        from patman import test_server
        from patman import test_settings
        from patman import test_status
        from patman import test_upstream
        from patman import test_workflow
        from u_boot_pylib import test_command

        test_common.clear_git_env()
        # Keep the tests away from the user's patchwork cache, and lore
        os.environ['PATMAN_PWCACHE'] = 'off'
        lore.set_mode('off')
        to_run = args.testname if args.testname not in [None, 'test'] else None
        result = test_util.run_test_suites(
            'patman', False, args.verbose, args.no_capture,
            args.test_preserve_dirs, None, to_run, None,
            [test_checkpatch.TestPatch, func_test.TestFunctional,
             'patman.settings', test_cseries.TestCseries,
             test_cseries.TestGetUpstreamBranch, test_status.TestSeriesStatus,
             test_upstream.TestUpstream, test_workflow.TestWorkflow,
             test_database.TestDatabase, test_review.TestReview,
             test_send.TestSend, test_settings.TestSettings,
             test_pwcache.TestPwCache, test_lore.TestLore,
             test_server.TestWebServer, test_command.TestRunInteractive])
        sys.exit(0 if result.wasSuccessful() else 1)

    # Process commits, produce patches files, check them, email them
    else:
        exit_code = control.do_patman(args)
        sys.exit(exit_code)


if __name__ == "__main__":
    sys.exit(run_patman())
