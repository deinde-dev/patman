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
from patman import lorelist
from patman import pwcache
from patman.patchwork import Patchwork
from patman.review import lookup


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

SERIES = {
    'id': 7, 'received_all': True,
    'cover_letter': {'id': 70, 'name': 'dm: Tidy up', 'msgid': '<cover@x>'},
    'patches': [{'id': 71, 'name': 'dm: First', 'msgid': '<p1@x>'},
                {'id': 72, 'name': 'dm: Second', 'msgid': '<p2@x>'}],
}


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

            # It is asked for again when rechecking
            client = Client(Response(200, gzip.compress(THREAD)))
            self.assertEqual(9, len(run(lre.get_thread(client, 'gone@x',
                                                       recheck=True))))
            self.assertEqual(1, len(client.urls))

            # A failure is tried again, but not remembered
            client = Client(Response(503), Response(503),
                            Response(200, gzip.compress(THREAD)))
            with mock.patch.object(lore.asyncio, 'sleep'):
                self.assertIsNone(run(lre.get_thread(client, 'busy@x')))
                self.assertEqual(9, len(run(lre.get_thread(client,
                                                           'busy@x'))))
            self.assertEqual(3, len(client.urls))

    def fake_patchwork(self, reads):
        """Make a Patchwork object with a fake server, recording the reads

        Each patch's comments on patchwork are the replies to it in THREAD
        """
        replies = {'patches/71/comments/': ['r1@x', 'r2@x'],
                   'patches/72/comments/': ['r4@x'],
                   'covers/70/comments/': ['r3@x']}

        def fake(subpath):
            reads.append(subpath)
            if subpath == 'series/7/':
                return SERIES
            if subpath.endswith('/comments/'):
                return [{'msgid': f'<{mid}>', 'content': 'From patchwork',
                         'submitter': {'name': 'Pat', 'email': 'pat@x'},
                         'date': ''} for mid in replies[subpath]]
            return {'id': int(subpath.split('/')[1]), 'state': 'new',
                    'project': {'list_id': 'u-boot.example.org'}}

        pwork = Patchwork.for_testing(fake)
        fake_lore = mock.Mock()
        fake_lore.thread = lore.parse_mbox(THREAD)

        async def get_thread(_client, msgid):
            fake_lore.msgid = msgid
            return fake_lore.thread
        fake_lore.get_thread = get_thread
        pwork.lore = fake_lore
        return pwork

    @staticmethod
    def sync(pwork, reads):
        """Sync series 7, giving the comments and the comment lists read"""
        reads.clear()
        cover, patches = run(pwork.series_get_state(None, '7', True, True))
        return ([c['content'].strip() for c in cover.comments],
                [[c['content'].strip()[:5] for c in pat.comments]
                 for pat in patches],
                sorted(r for r in reads if r.endswith('comments/')))

    def test_sync_use(self):
        """With 'use', a sync reads the comments from lore, else patchwork"""
        reads = []
        pwork = self.fake_patchwork(reads)
        with mock.patch.object(lore, 'MODE', 'use'):
            # The thread is read once, from its cover letter
            self.assertEqual((['For the series:\n\nTested-by: Simon Glass '
                               '<sjg@chromium.org>'],
                              [['> Fir', 'Thank'], ['Nit']], []),
                             self.sync(pwork, reads))
            self.assertEqual('<cover@x>', pwork.lore.msgid)

            # If lore does not have the thread, patchwork is asked
            all_three = ['covers/70/comments/', 'patches/71/comments/',
                         'patches/72/comments/']
            pwork.lore.thread = None
            self.assertEqual(all_three, self.sync(pwork, reads)[2])

            # So it is if lore is missing a patch, e.g. because it is behind
            pwork.lore.thread = [msg for msg in lore.parse_mbox(THREAD)
                                 if 'p2@x' not in str(msg['Message-ID'])]
            self.assertEqual(all_three, self.sync(pwork, reads)[2])

    def test_sync_hint(self):
        """With 'hint', comments come from patchwork when lore has new ones"""
        reads = []
        pwork = self.fake_patchwork(reads)
        now = [1000.0]
        pwork.cache = pwcache.Cache(os.path.join(self.tmpdir, 'pw.db'),
                                    clock=lambda: now[0])
        all_three = ['covers/70/comments/', 'patches/71/comments/',
                     'patches/72/comments/']
        with mock.patch.object(lore, 'MODE', 'hint'):
            # The comments come from patchwork, and the first time all are
            # read
            result = self.sync(pwork, reads)
            self.assertEqual((['From patchwork'],
                              [['From ', 'From '], ['From ']], all_three),
                             result)

            # Lore has no new replies, so none is read again
            self.assertEqual([], self.sync(pwork, reads)[2])

            # A new reply to patch 2 on lore means that only it is read
            extra = message('r5@x', 'Re: [PATCH 2/2] dm: Second', SIMON,
                            'Reviewed-by: Simon Glass <sjg@chromium.org>',
                            'p2@x', ['cover@x', 'p2@x'],
                            'u-boot.example.org')
            pwork.lore.thread = lore.parse_mbox(THREAD + extra.encode())
            self.assertEqual(['patches/72/comments/'],
                             self.sync(pwork, reads)[2])

            # Patchwork does not have that reply, but it is only read once
            self.assertEqual([], self.sync(pwork, reads)[2])

            # A reply sent to another list is not on patchwork, so is left
            # out
            other = message('r6@x', 'Re: [PATCH 1/2] dm: First', SIMON, 'Hi',
                            'p1@x', ['cover@x', 'p1@x'], 'linux.example.org')
            pwork.lore.thread = lore.parse_mbox(THREAD + other.encode())
            self.assertEqual([], self.sync(pwork, reads)[2])

            # After a while, all are read again, in case lore lacks some
            now[0] += lore.RECHECK + 1
            self.assertEqual(all_three, self.sync(pwork, reads)[2])

        # Without lore, all are read each time
        pwork.lore = False
        self.assertEqual(all_three, self.sync(pwork, reads)[2])

    def test_patch_info(self):
        """The place of a patch in its series comes from its subject"""
        msgs = {str(m['Message-ID']).strip('<>'): m
                for m in lore.parse_mbox(THREAD)}
        self.assertEqual((1, 2, 2, '[2/2] dm: Second'),
                         lore.patch_info(msgs['p2@x']))
        self.assertEqual((2, 1, 2, '[v2,1/2] dm: First'),
                         lore.patch_info(msgs['v2@x']))
        self.assertIsNone(lore.patch_info(msgs['cover@x']))
        self.assertIsNone(lore.patch_info(msgs['other@x']))

    def test_fill_from_lore(self):
        """Patches which patchwork lacks are taken from lore"""
        incomplete = {
            'id': 8, 'version': 1, 'total': 2, 'received_total': 1,
            'received_all': False,
            'cover_letter': {'id': 80, 'name': 'dm: Tidy up',
                             'msgid': '<cover@x>'},
            'patches': [{'id': 81, 'name': '[1/2] dm: First',
                         'msgid': '<p1@x>'}]}
        reads = []

        def fake(subpath):
            reads.append(subpath)
            if subpath == 'series/8/':
                return incomplete
            return {'id': 81, 'headers': {}, 'content': 'pw'}

        pwork = Patchwork.for_testing(fake)
        fake_lore = mock.Mock()
        fake_lore.thread = lore.parse_mbox(THREAD)

        async def get_thread(_client, _msgid):
            return fake_lore.thread
        fake_lore.get_thread = get_thread
        pwork.lore = fake_lore

        data = run(pwork.fill_from_lore(None, incomplete))
        self.assertEqual(['[1/2] dm: First', '[2/2] dm: Second'],
                         [p['name'] for p in data['patches']])
        self.assertEqual(['lore:p2@x'], data['lore_patches'])
        self.assertNotIn('lore_patches', incomplete)

        # The patch from lore can be read like any other, with its replies
        patch = run(pwork.get_patch(None, 'lore:p2@x'))
        self.assertEqual(('<p2@x>', 'Second', '', 'new'),
                         (patch['headers']['Message-ID'],
                          patch['content'].strip(), patch['diff'],
                          patch['state']))
        self.assertEqual(['<r4@x>'], [c['msgid'] for c in run(
            pwork.get_patch_comments(None, 'lore:p2@x'))])

        # The mbox has both patches, from lore, in order
        mbox = pwork.lore_mbox(data)
        self.assertEqual(
            ['[PATCH 1/2] dm: First', '[PATCH 2/2] dm: Second'],
            [str(m['Subject']) for m in lore.parse_mbox(mbox)])
        self.assertIsNone(pwork.lore_mbox(incomplete))

        # A review fills in the series, and goes ahead
        with mock.patch.object(lookup.tout, 'notice') as notice:
            data, _, _, count = lookup.fetch_series(pwork, '8')
        self.assertEqual(2, count)
        self.assertIn('Patchwork lacks [2/2] dm: Second; using the copy on '
                      'lore', [c.args[0] for c in notice.call_args_list])

        # If lore lacks the patch, the review cannot go ahead
        fake_lore.thread = [m for m in lore.parse_mbox(THREAD)
                            if 'p2@x' not in str(m['Message-ID'])]
        with mock.patch.object(lookup.tout, 'notice'):
            with self.assertRaises(ValueError) as exc:
                lookup.fetch_series(pwork, '8')
        self.assertIn('https://my-fake-url has 1 of 2', str(exc.exception))

        # A complete series is left alone, and lore is not asked
        fake_lore.get_thread = mock.Mock()
        done = dict(SERIES)
        self.assertIs(done, run(pwork.fill_from_lore(None, done)))
        fake_lore.get_thread.assert_not_called()

    def lore_list(self, thread=THREAD):
        """Make a LoreList which reads a thread given by the test"""
        llist = lorelist.LoreList('https://lore.kernel.org/my-list/')
        reads = []

        async def get_thread(_client, msgid, recheck=False):
            self.assertTrue(recheck)
            reads.append(msgid)
            messages = lore.parse_mbox(thread)
            if msgid not in {lorelist.msgid_of(msg) for msg in messages}:
                return None
            return messages
        llist.lore.get_thread = get_thread
        return llist, reads

    def test_lorelist_series(self):
        """A series is read from its thread on lore"""
        llist, reads = self.lore_list()
        self.assertEqual(('https://lore.kernel.org/my-list', 'my-list'),
                         (llist.url, llist.link_name))
        data = run(llist.get_series(None, '<cover@x>'))
        self.assertEqual(
            ('cover@x', '[0/2] dm: Tidy up', 1, 2, 2, True),
            (data['id'], data['name'], data['version'], data['total'],
             data['received_total'], data['received_all']))
        self.assertEqual('lore:cover@x', data['cover_letter']['id'])

        # The new version sent in reply to the old one is left out
        self.assertEqual(
            [('lore:p1@x', '[1/2] dm: First', '<p1@x>'),
             ('lore:p2@x', '[2/2] dm: Second', '<p2@x>')],
            [(p['id'], p['name'], p['msgid']) for p in data['patches']])
        self.assertEqual(['new', 'new'],
                         run(llist.get_series_patch_states(None, 'cover@x')))

        # Each reply goes to what it replies to, not to the cover letter
        def replies(func, item_id):
            return [c['msgid'] for c in run(func(None, item_id))]
        self.assertEqual(['<r3@x>'],
                         replies(llist.get_cover_comments, 'lore:cover@x'))
        self.assertEqual(['<r1@x>', '<r2@x>'],
                         replies(llist.get_patch_comments, 'lore:p1@x'))
        self.assertEqual(['<r4@x>'],
                         replies(llist.get_patch_comments, 'lore:p2@x'))

        patch = run(llist.get_patch(None, 'lore:p2@x'))
        self.assertEqual(('lore:p2@x', 'Second', '<p2@x>'),
                         (patch['id'], patch['content'].strip(),
                          patch['headers']['Message-ID']))
        cover = run(llist.get_cover(None, 'lore:cover@x'))
        self.assertEqual(('lore:cover@x', '[0/2] dm: Tidy up', 'Some tidy-ups'),
                         (cover['id'], cover['name'],
                          cover['content'].strip()))

        # The mbox has the patches in order, without the cover letter
        mbox = run(llist.get_series_mbox(None, 'cover@x'))
        self.assertEqual(
            ['[PATCH 1/2] dm: First', '[PATCH 2/2] dm: Second'],
            [str(m['Subject']) for m in lore.parse_mbox(mbox)])
        self.assertIsNone(llist.lore_mbox(data))
        self.assertIs(data, run(llist.fill_from_lore(None, data)))

        # The thread is read just once
        self.assertEqual(['cover@x'], reads)

        # A review reads the series the same way
        with mock.patch.object(lookup.tout, 'notice'):
            data, name, version, count = lookup.fetch_series(llist, 'cover@x')
        self.assertEqual(('dm: Tidy up', 1, 2), (name, version, count))

    def test_lorelist_single(self):
        """A patch on its own is a series, and only a series is accepted"""
        thread = ''.join([
            message('one@x', '[PATCH v2] libfdt: Fix it', FRED, 'Fix',
                    list_id='my-list.vger.kernel.org'),
            message('re@x', 'Re: [PATCH v2] libfdt: Fix it', SIMON, 'Nice',
                    'one@x', ['one@x']),
            # A patch sent in reply, e.g. a suggested change, is not part of
            # the series
            message('fix@x', '[PATCH] libfdt: Fix it better', SIMON, 'Fix',
                    'one@x', ['one@x']),
        ]).replace('Subject: [PATCH v2]',
                   'To: Fred <My-List@vger.kernel.org>\nSubject: [PATCH v2]',
                   1).encode('utf-8')
        llist, _ = self.lore_list(thread)
        data = run(llist.get_series(
            None, 'https://lore.kernel.org/my-list/one@x/T/#u'))
        self.assertEqual(('one@x', '[v2] libfdt: Fix it', 2, 1, None),
                         (data['id'], data['name'], data['version'],
                          data['total'], data['cover_letter']))
        self.assertEqual(['lore:one@x'], [p['id'] for p in data['patches']])
        self.assertEqual(
            {'name': 'my-list', 'link_name': 'my-list',
             'list_id': 'my-list.vger.kernel.org',
             'list_email': 'My-List@vger.kernel.org'}, data['project'])
        self.assertEqual(['<re@x>'], [c['msgid'] for c in run(
            llist.get_patch_comments(None, 'lore:one@x'))])

        llist, _ = self.lore_list()
        for msgid, msg in (
                ('p2@x', "Message 'p2@x' is patch 2 of 2; give the cover "
                 'letter or the first patch'),
                ('r1@x', "Message 'r1@x' is not a patch or cover letter"),
                ('gone@x', "Cannot read message 'gone@x' from "
                 'https://lore.kernel.org/my-list')):
            with self.assertRaises(ValueError) as exc:
                run(llist.get_series(None, msgid))
            self.assertEqual(msg, str(exc.exception))
        with self.assertRaises(ValueError) as exc:
            run(llist.query_series(None, 'dm: Tidy up'))
        self.assertIn('give the Message-ID', str(exc.exception))

    def test_lorelist_names(self):
        """A link to a series on lore is tidied into its Message-ID"""
        for link in ('a@b', '<a@b>', ' a@b ',
                     'https://lore.kernel.org/dtc/a@b/',
                     'https://lore.kernel.org/all/a@b/T/#u'):
            self.assertEqual('a@b', lorelist.parse_link(link))

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
