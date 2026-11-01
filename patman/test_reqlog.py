# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the log of requests sent to patchwork and lore"""

import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from u_boot_pylib import terminal
from patman import reqlog


class TestReqLog(unittest.TestCase):
    """Test the request log"""
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='patman.')
        self.path = os.path.join(self.tmpdir, 'cache', 'requests.log')
        self.env = mock.patch.dict(os.environ, {'PATMAN_REQLOG': self.path})
        self.env.start()
        reqlog.reset()

    def tearDown(self):
        self.env.stop()
        reqlog.reset()
        reqlog.set_command('')
        shutil.rmtree(self.tmpdir)

    def lines(self):
        """Read the lines of the log

        Returns:
            list of str: Lines
        """
        with open(self.path, encoding='utf-8') as inf:
            return inf.read().splitlines()

    def test_default_path(self):
        """The log is beside the cache, unless PATMAN_REQLOG says otherwise"""
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': '/c'}):
            del os.environ['PATMAN_REQLOG']
            self.assertEqual('/c/patman/requests.log', reqlog.default_path())
            os.environ['PATMAN_REQLOG'] = 'off'
            self.assertIsNone(reqlog.default_path())
            os.environ['PATMAN_REQLOG'] = '/x/req.log'
            self.assertEqual('/x/req.log', reqlog.default_path())

    def test_attempt(self):
        """Each try is logged with what came back, or what went wrong"""
        reqlog.set_command('series status')
        with reqlog.attempt('https://pw/api/1.2/series/1/', 1) as tried:
            tried.done(200, 615)
        with self.assertRaises(TimeoutError):
            with reqlog.attempt('https://pw/api/1.2/series/2/', 2):
                raise TimeoutError
        with self.assertRaises(OSError):
            with reqlog.attempt('https://pw/x', 3):
                raise OSError('Connection\n  reset')
        pid = os.getpid()
        lines = self.lines()
        self.assertEqual(3, len(lines))
        stamp = r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d'
        self.assertRegex(
            lines[0], f'^{stamp} pid={pid} series-status try=1 200 615B '
            r'\d+\.\d\ds https://pw/api/1.2/series/1/$')
        self.assertRegex(lines[1], r' try=2 TimeoutError 0B \d+\.\d\ds '
                         'https://pw/api/1.2/series/2/$')
        self.assertIn(' try=3 OSError: Connection reset 0B ', lines[2])

    def test_off(self):
        """Nothing is written when the log is off"""
        os.environ['PATMAN_REQLOG'] = 'off'
        reqlog.reset()
        with reqlog.attempt('https://pw/x', 1) as tried:
            tried.done(200, 1)
        self.assertFalse(os.path.exists(self.path))

    def test_rotate(self):
        """A log which grows too big is kept as '.1' and started again"""
        with mock.patch.object(reqlog, 'MAX_SIZE', 100):
            for _ in range(3):
                reqlog.write('x' * 60)
        self.assertEqual(['x' * 60], self.lines())
        with open(f'{self.path}.1', encoding='utf-8') as inf:
            self.assertEqual(2, len(inf.read().splitlines()))

    def test_cannot_write(self):
        """A log which cannot be written is reported once, then left alone"""
        os.makedirs(self.path)
        with terminal.capture() as (_, err):
            reqlog.write('one')
            reqlog.write('two')
        self.assertEqual(1, len(re.findall('Cannot write the request log',
                                           err.getvalue())))
