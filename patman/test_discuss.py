# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for talking through a reply in a Claude session"""

from datetime import datetime
import io
import os
import subprocess
from unittest import mock

from u_boot_pylib import terminal
from u_boot_pylib import tools

from patman import cmdline
from patman import database
from patman.review import discuss
from patman.review import replies
from patman.review import stored
from patman.test_common import CseriesTester

URL = 'https://claude.ai/code/session_01AbC'


class FakeTmux:  # pylint: disable=R0903
    """Stands in for tmux, recording the sessions it is asked for

    Properties:
        sessions (dict): key (str): session name
            value (list of str): command it runs
        cwds (dict): key (str): session name
            value (str): directory it runs in
        url (str or None): Link the sessions show
        trust (bool): True to have each session ask whether to trust its
            directory
        die (bool): True to have each session stop as soon as it starts
    """
    def __init__(self):
        self.sessions = {}
        self.cwds = {}
        self.url = URL
        self.trust = False
        self.die = False

    def run(self, *args):
        """Run a tmux command

        Args:
            args (str): Arguments for tmux

        Returns:
            subprocess.CompletedProcess: Result
        """
        out = ''
        if args[0] == 'list-sessions':
            out = '\n'.join(self.sessions)
        elif args[0] == 'new-session':
            name = args[args.index('-s') + 1]
            self.cwds[name] = args[args.index('-c') + 1]
            if not self.die:
                self.sessions[name] = list(args[args.index('--') + 1:])
        elif args[0] == 'capture-pane':
            out = (' Yes, I trust this folder\n' if self.trust else
                   f'/remote-control is active at\n  {self.url or ""}\n')
        elif args[0] == 'kill-session':
            self.sessions.pop(args[-1].lstrip('='), None)
        return subprocess.CompletedProcess(args, 0, out, '')


class TestDiscuss(CseriesTester):
    """Tests for discussing a reply"""
    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmpdir, 'cache')
        self.tmux = FakeTmux()
        patches = [
            mock.patch.dict(os.environ, {'XDG_CACHE_HOME': self.cache}),
            mock.patch.object(discuss, '_tmux', side_effect=self.tmux.run),
            mock.patch.object(discuss.shutil, 'which',
                              return_value='/usr/bin/tmux'),
            mock.patch.object(discuss.gitutil, 'get_top_level',
                              return_value=self.tmpdir),
            mock.patch.object(discuss.time, 'sleep'),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def add_reply(self, session=None):
        """Add a sent review with a reply to it

        Args:
            session (str or None): Agent session which wrote the review

        Returns:
            tuple:
                Cseries: Open cseries instance
                int: ID of the review
                int: ID of the reply
        """
        cser = self.get_database()
        db = cser.db
        db.upstream_add('dtc', 'https://dtc', lore_url='https://lore/dtc')
        sid = db.series_add('rev', 'dtc: Add a check', None)
        db.series_set_source(sid, 'review')
        db.series_set_upstream(sid, 'dtc')
        svid = db.ser_ver_add(sid, 1, link='c@x')
        rid = db.review_add(svid, 2, 'Please fix this', False,
                            datetime.now().isoformat())
        db.review_set_sent(rid, 'Please fix this', 'm1', 't1')
        if session:
            db.review_set_session(rid, session)
        reply_id = db.reply_add(
            rid, 'm2', '<a2@x>', '<p0@x>', 'Fred Bloggs <fred@x.com>',
            'sjg@chromium.org', '', 'Re: [PATCH 2/5] dtc: Add a check',
            'Mon, 1 Jan 2026 10:00:00 +0000', 'Why?',
            datetime.now().isoformat())
        db.reply_set_response(reply_id, 'Hi Fred,\n\nBecause.',
                              database.REPLY_SUGGESTED)
        db.commit()
        return cser, rid, reply_id

    def run_cmd(self, cser, *argv):
        """Run a 'patman review' command which handles replies

        Args:
            cser (Cseries): Open cseries instance
            argv (str): Arguments

        Returns:
            tuple:
                int: Return code
                str: Output
        """
        args = cmdline.parse_args(
            ['review', '--reviewer', 'Simon Glass <sjg@chromium.org>',
             *argv], config_fname=False)
        with terminal.capture() as (out, err):
            ret = replies.do_reply_cmd(args, cser)
        return ret, out.getvalue() + err.getvalue()

    def test_discuss(self):
        """A discussion starts in tmux, with the reply and how to finish"""
        cser, _, reply_id = self.add_reply()
        name = discuss.session_name(cser, reply_id)
        self.assertRegex(name, rf'^patman-[0-9a-f]{{6}}-reply-{reply_id}$')

        ret, out = self.run_cmd(cser, '--discuss-reply', str(reply_id))
        self.assertEqual(0, ret)
        title = 'patman: Fred Bloggs on [PATCH 2/5] dtc: Add a check'
        self.assertIn(f"discussing it in '{title}': {URL}", out)
        cmd = self.tmux.sessions[name]
        self.assertEqual(['claude', f'--remote-control={title}'], cmd[:2])
        self.assertEqual(self.tmpdir, self.tmux.cwds[name])

        # The first message says where the context is, and how to store the
        # response which is agreed
        prompt = cmd[-1]
        sdir = discuss.state_dir(name)
        self.assertIn("The series is 'dtc: Add a check'", prompt)
        self.assertIn('replied to the review of patch 2', prompt)
        self.assertIn('https://lore.kernel.org/all/a2@x/', prompt)
        self.assertNotIn('carries on', prompt)
        self.assertEqual(
            ('Please fix this', 'Why?', 'Hi Fred,\n\nBecause.'),
            tuple(tools.read_file(os.path.join(sdir, fname), binary=False)
                  for fname in ('review.txt', 'reply.txt', 'response.txt')))
        agreed = os.path.join(sdir, 'agreed.txt')
        self.assertIn(f'--set-response {reply_id} --file {agreed} -d', prompt)
        self.assertIn(f'cd {self.tmpdir} && ', prompt)

        # The page can find it, and starting again points to it
        self.assertEqual({reply_id: {'name': title, 'url': URL}},
                         discuss.discussions(cser))
        _, out = self.run_cmd(cser, '--discuss-reply', str(reply_id))
        self.assertIn('is already being discussed', out)
        self.assertEqual(1, len(self.tmux.sessions))

        # The session stores the agreed response
        tools.write_file(agreed, '\nHi Fred,\n\nAgreed.\n\n', binary=False)
        cmd = ['--set-response', str(reply_id), '--file', agreed]
        self.assertEqual(0, self.run_cmd(cser, *cmd)[0])
        reply = cser.db.reply_get(reply_id)
        self.assertEqual(('suggested', 'Hi Fred,\n\nAgreed.'),
                         (reply.status, reply.response))
        # With -d, as the session runs it, the response is drafted too
        with mock.patch.object(replies.gmail, 'check_available',
                               return_value=True), \
                mock.patch.object(replies.gmail, 'get_service'), \
                mock.patch.object(replies.gmail, 'create_draft',
                                  return_value={'id': 'd1'}) as create:
            self.run_cmd(cser, *cmd, '-d')
        self.assertEqual('Hi Fred,\n\nAgreed.', create.call_args.args[3])
        self.assertEqual(('draft', 'd1'),
                         (cser.db.reply_get(reply_id).status,
                          cser.db.reply_get(reply_id).draft_id))
        with mock.patch('sys.stdin', io.StringIO('From stdin\n')):
            self.run_cmd(cser, '--set-response', str(reply_id), '--file', '-')
        self.assertEqual('From stdin', cser.db.reply_get(reply_id).response)

        # Ending it closes the session and tidies up
        _, out = self.run_cmd(cser, '--end-discussion', str(reply_id))
        self.assertIn('ended the discussion', out)
        self.assertEqual({}, self.tmux.sessions)
        self.assertFalse(os.path.exists(sdir))
        self.assertEqual({}, discuss.discussions(cser))
        _, out = self.run_cmd(cser, '--end-discussion', str(reply_id))
        self.assertIn('is not being discussed', out)

    def test_discuss_review_session(self):
        """A discussion carries on from the session which wrote the review"""
        cser, _, reply_id = self.add_reply('sess1')
        wt_path = os.path.join(self.tmpdir, 'wt')
        session_file = os.path.join(self.tmpdir, 'sess1.jsonl')
        tools.write_file(session_file, '', binary=False)
        with mock.patch.object(stored, 'review_branch',
                               return_value='dtc-c-review') as branch, \
                mock.patch.object(stored, 'open_review_worktree',
                                  return_value=(wt_path, True)), \
                mock.patch.object(discuss, '_session_file',
                                  return_value=session_file), \
                mock.patch.object(discuss.gitutil,
                                  'remove_worktree') as remove:
            self.run_cmd(cser, '--discuss-reply', str(reply_id))
            name = discuss.session_name(cser, reply_id)
            self.assertEqual(('c@x', 'dtc', self.tmpdir), branch.call_args.args)
            cmd = self.tmux.sessions[name]
            self.assertEqual(['--resume', 'sess1', '--fork-session'], cmd[2:5])
            self.assertIn('carries on from the session', cmd[-1])
            self.assertEqual(wt_path, self.tmux.cwds[name])

            # Once the session has ended, e.g. with /exit, the worktree which
            # was added for it is removed
            self.tmux.sessions.clear()
            self.assertEqual({}, discuss.discussions(cser))
            remove.assert_called_once_with(self.tmpdir, wt_path)

            # If the review's session has gone, a new one is started
            os.unlink(session_file)
            _, out = self.run_cmd(cser, '--discuss-reply', str(reply_id))
            self.assertIn('has gone, so a new one is used', out)
            self.assertNotIn('--resume', self.tmux.sessions[name])

    def test_discuss_fail(self):
        """Problems starting a discussion or storing a response"""
        cser, _, reply_id = self.add_reply()
        name = discuss.session_name(cser, reply_id)

        # A session which stops at once is reported, and tidied up
        self.tmux.url = None
        self.tmux.die = True
        ret, out = self.run_cmd(cser, '--discuss-reply', str(reply_id))
        self.assertEqual((1, True), (ret, 'stopped as it started' in out))
        self.assertFalse(os.path.exists(discuss.state_dir(name)))
        self.tmux.die = False

        # Claude Code must trust the directory
        self.tmux.trust = True
        ret, out = self.run_cmd(cser, '--discuss-reply', str(reply_id))
        self.assertEqual(1, ret)
        self.assertIn(f'Claude Code does not trust {self.tmpdir} yet', out)
        self.assertEqual({}, self.tmux.sessions)
        self.tmux.trust = False

        # A session which gives no link is still used
        _, out = self.run_cmd(cser, '--discuss-reply', str(reply_id))
        self.assertIn('which has not given its link yet', out)
        self.assertEqual({reply_id: {'name': mock.ANY, 'url': None}},
                         discuss.discussions(cser))

        with self.assertRaises(ValueError) as exc:
            self.run_cmd(cser, '--set-response', str(reply_id))
        self.assertIn('--set-response needs --file', str(exc.exception))
        empty = os.path.join(self.tmpdir, 'empty.txt')
        tools.write_file(empty, '\n \n', binary=False)
        with self.assertRaises(ValueError) as exc:
            self.run_cmd(cser, '--set-response', str(reply_id), '--file',
                         empty)
        self.assertIn('The response is empty', str(exc.exception))

        self.run_cmd(cser, '--end-discussion', str(reply_id))
        with mock.patch.object(discuss.shutil, 'which', return_value=None):
            with self.assertRaises(ValueError) as exc:
                self.run_cmd(cser, '--discuss-reply', str(reply_id))
        self.assertIn('needs tmux', str(exc.exception))

        # Once the response is sent, it can be neither discussed nor set
        cser.db.reply_set_response(reply_id, 'Sent', database.REPLY_SENT)
        cser.db.commit()
        agreed = os.path.join(self.tmpdir, 'agreed.txt')
        tools.write_file(agreed, 'Hi', binary=False)
        for argv in (['--discuss-reply', str(reply_id)],
                     ['--set-response', str(reply_id), '--file', agreed]):
            with self.assertRaises(ValueError) as exc:
                self.run_cmd(cser, *argv)
            self.assertIn('has been sent', str(exc.exception))
