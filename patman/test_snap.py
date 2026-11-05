# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the checks made when patman runs in a strictly confined snap"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from u_boot_pylib import tools

from patman import snap


class TestSnap(unittest.TestCase):
    """Tests for the limits of the patman snap"""
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='patman-snap.')
        self.addCleanup(shutil.rmtree, self.tmpdir)
        self.snap_dir = os.path.join(self.tmpdir, 'snap')
        os.makedirs(os.path.join(self.snap_dir, 'meta'))
        self.set_confinement('strict')
        self.repo = os.path.join(self.tmpdir, 'repo')
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        env = mock.patch.dict(os.environ, {'SNAP': self.snap_dir,
                                           'SNAP_NAME': 'patman',
                                           'GIT_EDITOR': 'true'})
        env.start()
        self.addCleanup(env.stop)

    def set_confinement(self, confinement):
        """Write the snap's metadata, with the given confinement"""
        tools.write_file(os.path.join(self.snap_dir, 'meta', 'snap.yaml'),
                         f'name: patman\nconfinement: {confinement}\n',
                         binary=False)

    def git_config(self, key, value):
        """Set a git setting in the test repository"""
        subprocess.run(['git', 'config', key, value], cwd=self.repo,
                       check=True)

    def check_send(self, identity=None, smtp_server=None):
        """Run snap.check_send() on the test repository

        Returns:
            str or None: Error message, or None if there was no error
        """
        try:
            snap.check_send(identity, smtp_server, self.repo)
        except ValueError as exc:
            return str(exc)
        return None

    def test_confinement(self):
        """The confinement comes from the snap's metadata"""
        self.assertEqual('strict', snap.confinement())
        self.assertTrue(snap.is_strict())
        self.set_confinement('classic')
        self.assertEqual('classic', snap.confinement())
        self.assertFalse(snap.is_strict())

        # A snap whose metadata cannot be read is taken to be strict
        os.remove(os.path.join(self.snap_dir, 'meta', 'snap.yaml'))
        self.assertTrue(snap.is_strict())

        with mock.patch.dict(os.environ, {'SNAP': ''}):
            self.assertIsNone(snap.confinement())

    def test_check_cwd(self):
        """Running outside the directories the snap can reach is an error"""
        snap.check_setup()
        with mock.patch('os.getcwd', return_value=snap.VOID):
            with self.assertRaises(ValueError) as exc:
                snap.check_setup()
            self.assertIn('The patman snap cannot reach the directory it is '
                          'run in', str(exc.exception))
            self.assertIn(snap.GUIDE, str(exc.exception))

            # Outside the snap, nothing is checked
            self.set_confinement('classic')
            snap.check_setup()

    def test_check_plugs(self):
        """Settings which the snap is not allowed to read are reported"""
        home = os.path.join(self.tmpdir, 'home')
        os.makedirs(os.path.join(home, '.config', 'git'))
        tools.write_file(os.path.join(home, '.patman'), '', binary=False)
        with mock.patch.dict(os.environ, {'HOME': home}):
            snap.check_setup()
            with mock.patch('patman.snap.open', create=True,
                            side_effect=PermissionError):
                with self.assertRaises(ValueError) as exc:
                    snap.check_setup()
            self.assertIn("cannot read ~/.patman: connect its dot-patman "
                          "interface with 'sudo snap connect "
                          "patman:dot-patman'", str(exc.exception))
            with mock.patch('os.listdir', side_effect=PermissionError):
                with self.assertRaises(ValueError) as exc:
                    snap.check_setup()
            self.assertIn('cannot read ~/.config/git: connect its '
                          'dot-gitconfig interface', str(exc.exception))

    def test_check_send_editor(self):
        """An editor which the snap cannot run is reported"""
        self.git_config('sendemail.smtpServer', 'smtp.example.com')
        self.assertIsNone(self.check_send())
        with mock.patch.dict(os.environ, {'GIT_EDITOR': 'no-such-editor -w'}):
            self.assertIn("Cannot run your editor 'no-such-editor -w'",
                          self.check_send())

            # Outside the snap, nothing is checked
            self.set_confinement('classic')
            self.assertIsNone(self.check_send())

    def test_check_send_smtp(self):
        """git send-email needs an SMTP server which the snap can reach"""
        self.assertIn('No SMTP server is set', self.check_send())
        self.assertIsNone(self.check_send(smtp_server='smtp.example.com'))

        self.git_config('sendemail.smtpServer', '/no/such/msmtp')
        self.assertIn("Cannot run '/no/such/msmtp'", self.check_send())
        self.assertIsNone(self.check_send(smtp_server='smtp.example.com'))
        self.git_config('sendemail.smtpServer', shutil.which('true'))
        self.assertIsNone(self.check_send())

        # An identity's own server is used in preference
        self.git_config('sendemail.smtpServer', 'smtp.example.com')
        self.git_config('sendemail.work.smtpServer', '/no/such/sendmail')
        self.assertIsNone(self.check_send())
        self.assertIn("Cannot run '/no/such/sendmail'",
                      self.check_send(identity='work'))
        self.git_config('sendemail.identity', 'work')
        self.assertIn("Cannot run '/no/such/sendmail'", self.check_send())

    def test_check_send_credential(self):
        """A credential helper which the snap cannot run is reported"""
        self.git_config('sendemail.smtpServer', 'smtp.example.com')
        self.git_config('credential.helper', 'libsecret')
        # Without an SMTP user, no password is needed
        self.assertIsNone(self.check_send())

        self.git_config('sendemail.smtpUser', 'me')
        self.assertIn("Cannot run the credential helper 'libsecret'",
                      self.check_send())
        self.git_config('credential.helper', 'cache --timeout=600')
        self.assertIsNone(self.check_send())
        self.git_config('credential.helper', '!no-such-helper get')
        self.assertIn("helper '!no-such-helper get'", self.check_send())
        self.git_config('credential.helper', '!true')
        self.assertIsNone(self.check_send())

        # With a password, the helper is not used
        self.git_config('credential.helper', 'libsecret')
        self.git_config('sendemail.smtpPass', 'secret')
        self.assertIsNone(self.check_send())
