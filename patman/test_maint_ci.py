# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for testing the queue and running CI on it"""

import io
import json
import os
from unittest import mock

from u_boot_pylib import command
from u_boot_pylib import terminal

from patman import cmdline
from patman import control
from patman import maint
from patman import maint_ci
from patman.test_maint import DTC, FRED, ME, RepoTestCase

# Trailers which pass the dtc rules
GOOD = [f'Signed-off-by: {FRED}', 'Message-ID: <1@x.org>',
        f'Signed-off-by: {ME}']


class TestMaintCi(RepoTestCase):
    """Test testing the queue and running CI on it"""
    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmpdir, 'ci.git')
        self.git('init', '-q', '--bare', self.remote, cwd=self.tmpdir)
        self.git('remote', 'add', 'ci', self.remote)

    def add_file(self, fname, text, trailers=None):
        """Add a commit which writes a file

        Args:
            fname (str): Name of the file
            text (str): What to write in it
            trailers (list of str or None): Trailers, or None for GOOD

        Return:
            str: Hash of the commit
        """
        with open(os.path.join(self.repo, fname), 'w',
                  encoding='utf-8') as outf:
            outf.write(text)
        self.git('add', fname)
        return self.commit(f'Add {fname}', FRED,
                           GOOD if trailers is None else trailers)

    def run_ci(self, states, rules=None, **kwargs):
        """Run CI, with each place giving its states in turn

        Args:
            states (dict): For each system, e.g. 'github', a list of states,
                one for each time it is asked
            rules (Rules or None): Rules, or None for the dtc ones with the
                ci remote
            kwargs: Other arguments for do_ci()

        Return:
            tuple: Exit code, output, number of times it waited
        """
        sleeps = []

        def state(system):
            def get(repo, sha):
                self.assertEqual(self.git('rev-parse', 'main').strip(), sha)
                got = states[system].pop(0) if len(states[system]) > 1 else \
                    states[system][0]
                return maint_ci.CiResult(system, repo, got,
                                         f'https://{system}/run'
                                         if got != maint_ci.MISSING else None)
            return get

        rules = rules or self.rules(ci_push='ci +{branch}:ci')
        with mock.patch.dict(maint_ci.STATE_FUNCS,
                             {'github': state('github'),
                              'gitlab': state('gitlab')}), \
                terminal.capture() as (out, _):
            ret = maint_ci.do_ci(rules, git_dir=self.repo,
                                 sleep=sleeps.append, **kwargs)
        return ret, out.getvalue(), len(sleeps)

    def ci_tip(self):
        """Get the commit which the ci branch of the remote is at"""
        return command.output('git', '--git-dir', self.remote, 'rev-parse',
                              '--verify', '-q', 'refs/heads/ci',
                              raise_on_error=False).strip()

    def test_settings(self):
        """The test commands and CI pushes are read from the settings"""
        rules = self.rules(test='make check; meson test -C build',
                           ci_push='david +{branch}:ci; gl {branch}:ci')
        self.assertEqual(['make check', 'meson test -C build'], rules.test)
        self.assertEqual([('david', '+{branch}:ci'), ('gl', '{branch}:ci')],
                         rules.ci_push)
        self.assertEqual(([], []), (self.rules().test, self.rules().ci_push))
        with self.assertRaises(ValueError) as exc:
            self.rules(ci_push='david')
        self.assertEqual("Each entry of the maint setting 'ci_push' needs a "
                         "remote and a refspec, e.g. 'david +{branch}:ci', "
                         "not 'david'", str(exc.exception))

    def test_test(self):
        """The tests run in a worktree of their own, stopping at a failure"""
        commit = self.add_file('a.txt', 'one\n')
        self.add_file('b.txt', 'local change\n')
        self.git('reset', '-q', '--hard', 'HEAD~1')
        # A change which is not committed is not tested
        with open(os.path.join(self.repo, 'a.txt'), 'w',
                  encoding='utf-8') as outf:
            outf.write('two\n')

        def test(*cmds):
            rules = self.rules(test='; '.join(cmds))
            with terminal.capture() as (out, _):
                ret = maint_ci.do_test(rules, self.repo)
            return ret, out.getvalue()

        self.assertEqual(
            (0, f'Testing main at {commit[:12]}\n'
                '  ok   grep -q one a.txt\n  ok   test ! -f b.txt\n'),
            test('grep -q one a.txt', 'test ! -f b.txt'))

        # The output of a command which fails is shown
        ret, out = test('true', 'echo hello && echo oops >&2 && false',
                        'echo never')
        self.assertEqual(1, ret)
        self.assertEqual(f'Testing main at {commit[:12]}\n'
                         '  ok   true\n'
                         '  FAIL echo hello && echo oops >&2 && false\n'
                         '       hello\n'
                         '       oops\n', out)

        # The worktree is removed and the checkout is untouched
        self.assertEqual(1, len(self.git('worktree', 'list').splitlines()))
        self.assertEqual(' M a.txt\n', self.git('status', '--porcelain'))

        with self.assertRaises(ValueError) as exc:
            maint_ci.do_test(self.rules(), self.repo)
        self.assertEqual("No tests to run: set 'test' in the [maint] section "
                         "of the settings, e.g. 'test: make check'",
                         str(exc.exception))

    def test_ci(self):
        """The queue is checked, pushed, and CI is waited for"""
        commit = self.add_file('a.txt', 'one\n')
        pend, good, bad, miss = (maint_ci.PENDING, maint_ci.SUCCESS,
                                 maint_ci.FAILURE, maint_ci.MISSING)

        # A dry run says where the branch would go
        ret, out, _ = self.run_ci({}, dry_run=True)
        self.assertEqual(0, ret)
        self.assertTrue(out.endswith(
            f'Would push {commit[:12]} to ci +main:ci\n'), out)
        self.assertEqual('', self.ci_tip())

        # CI starts after a while, then passes
        ret, out, waits = self.run_ci({'github': [miss, pend, good],
                                       'gitlab': [pend, good]})
        self.assertEqual((0, 2), (ret, waits), out)
        self.assertEqual(commit, self.ci_tip())
        self.assertTrue(out.endswith(
            f'Pushed {commit[:12]} to ci +main:ci\n'
            '  ok      github https://github/run\n'
            '  ok      gitlab https://gitlab/run\n'), out)

        # A failure anywhere is a failure
        ret, out, _ = self.run_ci({'github': [good], 'gitlab': [pend, bad]})
        self.assertEqual(1, ret)
        self.assertIn('  FAIL    gitlab https://gitlab/run\n', out)

        # CI may never start in one place
        with mock.patch.object(maint_ci, 'CI_START', 0):
            ret, out, _ = self.run_ci({'github': [good], 'gitlab': [miss]})
        self.assertEqual(1, ret)
        self.assertIn('  no run  gitlab dgibson/dtc\n', out)

        # Pushing without waiting
        ret, out, waits = self.run_ci({'github': [pend], 'gitlab': [pend]},
                                      wait=False)
        self.assertEqual((0, 0), (ret, waits))
        self.assertTrue(out.endswith('to ci +main:ci\n'), out)

        # Nothing goes out if the check fails
        broken = self.add_file('b.txt', 'two\n', [f'Signed-off-by: {FRED}'])
        ret, out, _ = self.run_ci({})
        self.assertEqual(1, ret)
        self.assertIn(f'  FAIL {broken[:12]} Add b.txt\n', out)
        self.assertTrue(out.endswith('Not pushed, since the check failed\n'))
        self.assertEqual(commit, self.ci_tip())

    def test_ci_problems(self):
        """Problems with pushing or with the settings are reported"""
        self.add_file('a.txt', 'one\n')
        ret, out, _ = self.run_ci({}, self.rules(ci_push='nowhere main:ci'))
        self.assertEqual(1, ret)
        self.assertIn('Cannot push to nowhere: ', out)

        with self.assertRaises(ValueError) as exc:
            self.run_ci({}, self.rules())
        self.assertIn("Nowhere to run CI: set 'ci_push'", str(exc.exception))

        # Without anywhere to watch, CI is not waited for
        rules = self.rules(ci_push='ci +{branch}:ci', github='', gitlab='')
        ret, out, _ = self.run_ci({}, rules)
        self.assertEqual(0, ret)
        self.assertTrue(out.endswith(
            "Not waiting for CI: set 'github' or 'gitlab' in the [maint] "
            'section to say where it runs\n'), out)

        # There is nothing to send when the branch is level with upstream
        self.git('update-ref', 'refs/remotes/us/main', 'main')
        ret, out, _ = self.run_ci({})
        self.assertEqual(0, ret)
        self.assertNotIn('Pushed', out)

    def test_states(self):
        """The state of CI is read from GitHub and GitLab"""
        sha = 'a' * 40
        runs = ('completed\tsuccess\thttps://gh/1\n'
                'completed\tfailure\thttps://gh/2\n'
                'in_progress\t\thttps://gh/3\n')
        with mock.patch.object(maint_ci.command, 'output',
                               return_value=runs) as out:
            self.assertEqual(
                maint_ci.CiResult('github', 'dgibson/dtc', maint_ci.FAILURE,
                                  'https://gh/2'),
                maint_ci.github_state('dgibson/dtc', sha))
        self.assertIn(f'repos/dgibson/dtc/actions/runs?head_sha={sha}',
                      out.call_args.args)
        for runs, want in (
                ('completed\tsuccess\thttps://gh/1\n'
                 'in_progress\t\thttps://gh/3\n', maint_ci.PENDING),
                ('completed\tskipped\thttps://gh/1\n', maint_ci.SUCCESS),
                ('', maint_ci.MISSING)):
            with mock.patch.object(maint_ci.command, 'output',
                                   return_value=runs):
                self.assertEqual(want, maint_ci.github_state(
                    'dgibson/dtc', sha).state)

        def urlopen(pipelines):
            def get(url, timeout):
                self.assertEqual(
                    'https://gitlab.com/api/v4/projects/dgibson%2Fdtc/'
                    f'pipelines?sha={sha}', url)
                self.assertTrue(timeout)
                return io.BytesIO(json.dumps(pipelines).encode())
            return get

        for pipelines, want in (
                ([{'status': 'success', 'web_url': 'u1'}], maint_ci.SUCCESS),
                ([{'status': 'running', 'web_url': 'u1'}], maint_ci.PENDING),
                ([{'status': 'canceled', 'web_url': 'u1'}], maint_ci.FAILURE),
                ([], maint_ci.MISSING)):
            with mock.patch.object(maint_ci.urllib.request, 'urlopen',
                                   urlopen(pipelines)):
                self.assertEqual(want, maint_ci.gitlab_state(
                    'dgibson/dtc', sha).state)

        # A place which cannot be read is still running, as far as is known
        rules = self.rules()
        with mock.patch.object(maint_ci.command, 'output',
                               side_effect=command.CommandExc(
                                   'gh: not logged in', None)), \
                mock.patch.object(maint_ci.urllib.request, 'urlopen',
                                  side_effect=OSError('no network')):
            self.assertEqual(
                [maint_ci.CiResult('github', 'dgibson/dtc', maint_ci.PENDING,
                                   error='gh: not logged in'),
                 maint_ci.CiResult('gitlab', 'dgibson/dtc', maint_ci.PENDING,
                                   error='no network')],
                maint_ci.ci_states(rules, sha))
        with terminal.capture() as (out, _):
            self.assertFalse(maint_ci.show_ci([maint_ci.CiResult(
                'gitlab', 'dgibson/dtc', maint_ci.PENDING,
                error='no network')]))
        self.assertEqual('  running gitlab dgibson/dtc: no network\n',
                         out.getvalue())

    def test_command(self):
        """'patman maint test' and 'patman maint ci' use the [maint] rules"""
        self.add_file('a.txt', 'one\n')
        test_args = cmdline.parse_args(['maint', 'test'], config_fname=False)
        ci_args = cmdline.parse_args(['maint', 'ci', '-n'], config_fname=False)
        orig = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(maint.settings.maint, {
                    **DTC, 'test': 'grep -q one a.txt',
                    'ci_push': 'ci +{branch}:ci'}, clear=True), \
                    terminal.capture() as (out, _):
                self.assertEqual(0, control.do_maint(test_args))
                self.assertEqual(0, control.do_maint(ci_args))
        finally:
            os.chdir(orig)
        self.assertIn('  ok   grep -q one a.txt\n', out.getvalue())
        self.assertIn('Would push ', out.getvalue())
        args = cmdline.parse_args(['maint', 'ci', '-W', '-b', 'main'],
                                  config_fname=False)
        self.assertEqual((False, False, 'main'),
                         (args.wait, args.dry_run, args.branch))
