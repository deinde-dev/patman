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
        self.assertEqual([('david', ['+{branch}:ci']), ('gl', ['{branch}:ci'])],
                         rules.ci_push)
        self.assertEqual(([], []), (self.rules().test, self.rules().ci_push))
        with self.assertRaises(ValueError) as exc:
            self.rules(ci_push='david')
        self.assertEqual("Each entry of the maint setting 'ci_push' needs a "
                         "remote and one or more refspecs, e.g. "
                         "'david +{branch}:ci', not 'david'",
                         str(exc.exception))
        rules = self.rules(publish='us {branch}:main {branch}:master; '
                                   'david {branch}:main')
        self.assertEqual([('us', ['{branch}:main', '{branch}:master']),
                          ('david', ['{branch}:main'])], rules.publish)
        with self.assertRaises(ValueError) as exc:
            self.rules(publish='us')
        self.assertIn("'publish' needs a remote and one or more refspecs, "
                      "e.g. 'us {branch}:main'", str(exc.exception))

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

    def setup_publish(self):
        """Make bare repos to publish to, in place of the real remotes

        Return:
            dict: Path of the bare repo for each remote
        """
        bare = {}
        for remote in ('us', 'david', 'gl'):
            bare[remote] = os.path.join(self.tmpdir, f'{remote}.git')
            self.git('init', '-q', '--bare', bare[remote], cwd=self.tmpdir)
            if remote == 'gl':
                self.git('remote', 'add', remote, bare[remote])
            else:
                self.git('remote', 'set-url', remote, bare[remote])
        # Each has the upstream branch as it is now
        for remote in bare:
            self.git('push', '-q', remote, 'refs/remotes/us/main:refs/heads/main')
        self.git('fetch', '-q', '--all')
        return bare

    def publish(self, state, answer='y', rules=None, **kwargs):
        """Publish the queue, with CI in the given state

        Args:
            state (str): State of CI everywhere, e.g. maint_ci.SUCCESS
            answer (str or None): Answer to give when asked, or None if
                there should be no question
            rules (Rules or None): Rules, or None for the dtc ones with the
                test remotes
            kwargs: Other arguments for do_publish()

        Return:
            tuple: Exit code, output
        """
        def get(system):
            return lambda repo, sha: maint_ci.CiResult(system, repo, state,
                                                       f'https://{system}/run')

        rules = rules or self.rules(
            upstream='us main',
            publish='us {branch}:main {branch}:master; david {branch}:main; '
                    'gl {branch}:main')
        with mock.patch.dict(maint_ci.STATE_FUNCS, {'github': get('github'),
                                                    'gitlab': get('gitlab')}), \
                mock.patch('builtins.input', return_value=answer) as ask, \
                terminal.capture() as (out, _):
            ret = maint_ci.do_publish(rules, git_dir=self.repo, **kwargs)
        if answer is None:
            ask.assert_not_called()
        return ret, out.getvalue()

    def tip(self, bare, branch='main'):
        """Get the commit which a branch of a bare repo is at"""
        return command.output('git', '--git-dir', bare, 'rev-parse',
                              '--verify', '-q', f'refs/heads/{branch}',
                              raise_on_error=False).strip()

    def test_publish(self):
        """The queue is published once CI has passed on it, after asking"""
        bare = self.setup_publish()
        base = self.tip(bare['us'])
        commit = self.add_file('a.txt', 'one\n')
        where = 'us main:main main:master, david main:main, gl main:main'

        # Not until CI has passed on the last commit
        ret, out = self.publish(maint_ci.PENDING, None)
        self.assertEqual(1, ret)
        self.assertIn('  running github https://github/run\n', out)
        self.assertTrue(out.endswith(
            f'Not published, since CI has not passed on {commit[:12]}: run '
            "'patman maint ci'\n"), out)

        # Not if the answer is no, nor in a dry run
        ret, out = self.publish(maint_ci.SUCCESS, 'n')
        self.assertEqual(1, ret)
        self.assertTrue(out.endswith('Not published\n'), out)
        after = mock.Mock()
        ret, out = self.publish(maint_ci.SUCCESS, None, dry_run=True,
                                after=after)
        self.assertEqual(0, ret)
        after.assert_not_called()
        self.assertTrue(out.endswith(f'Would publish {commit[:12]} to '
                                     f'{where}\n'), out)
        self.assertEqual([base] * 3, [self.tip(path)
                                      for path in bare.values()])

        after = mock.Mock()
        ret, out = self.publish(maint_ci.SUCCESS, after=after)
        self.assertEqual(0, ret, out)
        self.assertTrue(out.endswith(f'Published {commit[:12]} to {where}\n'),
                        out)
        after.assert_called_once_with(base, commit)
        self.assertEqual([commit] * 4, [self.tip(bare['us']),
                                        self.tip(bare['us'], 'master'),
                                        self.tip(bare['david']),
                                        self.tip(bare['gl'])])

        # Then there is nothing more to publish, so no question
        self.assertEqual(0, self.publish(maint_ci.SUCCESS, None)[0])

    def test_publish_partly(self):
        """Publishing again after a failure carries on where it stopped"""
        bare = self.setup_publish()
        base = self.tip(bare['us'])
        commit = self.add_file('a.txt', 'one\n')
        self.git('remote', 'set-url', '--push', 'david',
                 os.path.join(self.tmpdir, 'missing.git'))
        ret, out = self.publish(maint_ci.SUCCESS)
        self.assertEqual(1, ret)
        self.assertIn('Cannot push to david: ', out)
        self.assertEqual((commit, base, base), (self.tip(bare['us']),
                                                self.tip(bare['david']),
                                                self.tip(bare['gl'])))

        self.git('remote', 'set-url', '--push', 'david', bare['david'])
        ret, out = self.publish(maint_ci.SUCCESS)
        self.assertEqual(0, ret, out)
        self.assertTrue(out.endswith(
            f'Published {commit[:12]} to david main:main, gl main:main\n'),
            out)
        self.assertEqual((commit, commit), (self.tip(bare['david']),
                                            self.tip(bare['gl'])))

    def test_publish_refused(self):
        """Nothing is published if the check fails or there is nowhere to
        publish"""
        bare = self.setup_publish()
        base = self.tip(bare['us'])
        broken = self.add_file('a.txt', 'one\n', [f'Signed-off-by: {FRED}'])
        ret, out = self.publish(maint_ci.SUCCESS, None)
        self.assertEqual(1, ret)
        self.assertIn(f'  FAIL {broken[:12]} Add a.txt\n', out)
        self.assertTrue(out.endswith('Not published, since the check '
                                     'failed\n'))
        self.assertEqual(base, self.tip(bare['us']))

        # A branch which is behind the upstream has nothing to publish
        self.git('push', '-q', 'us', 'main:main')
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.assertEqual(0, self.publish(maint_ci.SUCCESS, None)[0])
        self.assertEqual(broken, self.tip(bare['us']))
        self.assertEqual(base, self.tip(bare['david']))

        # A remote which cannot be fetched stops it
        self.git('remote', 'set-url', 'gl',
                 os.path.join(self.tmpdir, 'missing.git'))
        ret, out = self.publish(maint_ci.SUCCESS, None)
        self.assertEqual(1, ret)
        self.assertIn('Cannot fetch gl: ', out)

        with self.assertRaises(ValueError) as exc:
            self.publish(maint_ci.SUCCESS, None,
                         self.rules(upstream='us main'))
        self.assertEqual("Nowhere to publish: set 'publish' in the [maint] "
                         "section of the settings, e.g. "
                         "'publish: us {branch}:main'", str(exc.exception))

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

        # Publishing asks first, unless given -y
        self.setup_publish()
        pub_args = cmdline.parse_args(['maint', 'publish', '-n'],
                                      config_fname=False)
        self.assertEqual((True, False), (pub_args.dry_run, pub_args.yes))
        self.assertTrue(cmdline.parse_args(['maint', 'publish', '-y'],
                                           config_fname=False).yes)
        os.chdir(self.repo)
        try:
            with mock.patch.dict(maint.settings.maint, {
                    **DTC, 'upstream': 'us main', 'publish': 'us {branch}:main',
                    'github': '', 'gitlab': ''}, clear=True), \
                    terminal.capture() as (out, _):
                self.assertEqual(0, control.do_maint(pub_args))
        finally:
            os.chdir(orig)
        self.assertIn('Would publish ', out.getvalue())
