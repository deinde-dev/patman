# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for reading mailing-list threads from lore"""

import asyncio
import gzip
import os
import shutil
import tempfile
import unittest
from unittest import mock

from patman import lore
from patman import pwcache


def run(coro):
    """Run a coroutine in a loop of its own, leaving the thread's loop alone"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# pylint: disable-next=R0913
def message(msgid, subject, sender, body, reply_to=None, refs=None,
            list_id=None):
    """Make the text of an email, as it appears in an mbox file"""
    lines = ['From mboxrd@z Thu Jan  1 00:00:00 1970',
             f'From: {sender}',
             f'Subject: {subject}',
             'Date: Tue, 14 Oct 2026 10:38:38 +0200',
             f'Message-ID: <{msgid}>']
    if reply_to:
        lines.append(f'In-Reply-To: <{reply_to}>')
    if refs:
        lines.append('References: ' + ' '.join(f'<{ref}>' for ref in refs))
    if list_id:
        lines.append(f'List-Id: <{list_id}>')
    return '\n'.join(lines) + '\n\n' + body + '\n\n'


FRED = 'Fred Bloggs <fred@example.com>'
SIMON = 'Simon Glass <sjg@chromium.org>'

# A series with a cover letter and two patches, and the replies to it
THREAD = ''.join([
    message('cover@x', '[PATCH 0/2] dm: Tidy up', FRED, 'Some tidy-ups'),
    message('p1@x', '[PATCH 1/2] dm: First', FRED, 'First',
            'cover@x', ['cover@x']),
    message('p2@x', '[PATCH 2/2] dm: Second', FRED, 'Second',
            'cover@x', ['cover@x']),
    message('r1@x', 'Re: [PATCH 1/2] dm: First', SIMON,
            '> First\n\nReviewed-by: Simon Glass <sjg@chromium.org>',
            'p1@x', ['cover@x', 'p1@x']),
    message('r2@x', 'Re: [PATCH 1/2] dm: First', FRED, 'Thanks',
            'r1@x', ['cover@x', 'p1@x', 'r1@x']),
    message('r3@x', 'Re: [PATCH 0/2] dm: Tidy up', SIMON,
            'For the series:\n\nTested-by: Simon Glass <sjg@chromium.org>',
            'cover@x', ['cover@x']),
    # A reply to a message which lore does not have, found by References
    message('r4@x', 'Re: [PATCH 2/2] dm: Second', SIMON, 'Nit',
            'gone@x', ['cover@x', 'p2@x', 'gone@x']),
    # A new version sent in reply to the old one, which is not a comment
    message('v2@x', '[PATCH v2 1/2] dm: First', FRED, 'First, again',
            'p1@x', ['cover@x', 'p1@x']),
    # Nothing to do with the series
    message('other@x', 'Hello', FRED, 'Hi'),
]).encode('utf-8')

class Response:
    """Pretend response from lore"""
    def __init__(self, status, content=b''):
        self.status = status
        self.content = content

    async def read(self):
        """Get the content"""
        return self.content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Client:  # pylint: disable=R0903
    """Pretend aiohttp session, giving each response in turn"""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **_kwargs):
        """Start a request"""
        self.urls.append(url)
        return self.responses.pop(0)


class TestLore(unittest.TestCase):
    """Test reading threads from lore"""
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='patman.')

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_comments_for(self):
        """Replies go to the message they reply to, however deep"""
        messages = lore.parse_mbox(THREAD)
        self.assertEqual(9, len(messages))
        found = lore.comments_for(messages, ['<cover@x>', '<p1@x>', 'p2@x'])
        self.assertEqual(
            {'cover@x': ['r3@x'], 'p1@x': ['r1@x', 'r2@x'],
             'p2@x': ['r4@x']},
            {mid: [bare(c['msgid']) for c in comments]
             for mid, comments in found.items()})

    def test_to_comment(self):
        """A message gives the fields of a patchwork comment"""
        comment = lore.comments_for(lore.parse_mbox(THREAD),
                                    ['<p1@x>'])['p1@x'][0]
        self.assertEqual(
            (None, '<r1@x>', '2026-10-14T08:38:38',
             {'name': 'Simon Glass', 'email': 'sjg@chromium.org'},
             'Re: [PATCH 1/2] dm: First',
             'https://lore.kernel.org/all/r1@x/'),
            (comment['id'], comment['msgid'], comment['date'],
             comment['submitter'], comment['subject'], comment['web_url']))
        self.assertIn('Reviewed-by: Simon Glass', comment['content'])
        self.assertEqual('<p1@x>', comment['headers']['In-Reply-To'])

    def test_get_thread(self):
        """A thread is read, and one which lore lacks is remembered"""
        lre = lore.Lore()
        client = Client(Response(200, gzip.compress(THREAD)))
        self.assertEqual(9, len(run(lre.get_thread(client, '<cover@x>'))))
        self.assertEqual(['https://lore.kernel.org/all/cover@x/t.mbox.gz'],
                         client.urls)

        cache = pwcache.Cache(os.path.join(self.tmpdir, 'pw.db'))
        with mock.patch.object(pwcache, 'get', return_value=cache):
            client = Client(Response(404))
            self.assertIsNone(run(lre.get_thread(client, 'gone@x')))
            self.assertIsNone(run(lre.get_thread(client, 'gone@x')))
            self.assertEqual(1, len(client.urls))

            # A failure is tried again, but not remembered
            client = Client(Response(503), Response(503),
                            Response(200, gzip.compress(THREAD)))
            with mock.patch.object(lore.asyncio, 'sleep'):
                self.assertIsNone(run(lre.get_thread(client, 'busy@x')))
                self.assertEqual(9, len(run(lre.get_thread(client,
                                                           'busy@x'))))
            self.assertEqual(3, len(client.urls))

    def test_set_mode(self):
        """The mode must be one which is known"""
        with mock.patch.object(lore, 'MODE', 'hint'):
            lore.set_mode('use')
            self.assertEqual('use', lore.MODE)
            with self.assertRaises(ValueError) as exc:
                lore.set_mode('sometimes')
            self.assertIn("Unknown lore mode 'sometimes'", str(exc.exception))

def bare(msgid):
    """Remove the angle brackets from a Message-ID"""
    return msgid.strip('<>')
