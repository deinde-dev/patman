#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Run 'patman serve' with made-up series, for a demo of the page

Usage: serve_demo.py [--port PORT]

This creates a throwaway database and git repo, and runs the server with a
fake patchwork which holds a few made-up series. The jobs which the page
starts, such as reviews, run a stand-in 'patman' package, which calls this
script, so that they write plausible results to the database without running
the review agent, reading patchwork or touching Gmail. Nothing is sent
anywhere.

The series and people are made up, as are the reviews.
"""

import argparse
from datetime import datetime, timedelta, timezone
import email.utils
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..'))

# pylint: disable=C0413
from patman.database import Database, Pcommit, SerVer
from patman.patchwork import Patchwork
from patman import pwcache

PW_URL = 'https://patchwork.example.org'
UPSTREAM = 'us'


def _ago(hours):
    """Get a patchwork time some hours ago, in UTC"""
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    return when.strftime('%Y-%m-%dT%H:%M:%S')


def _patch(pid, name):
    """Make a patch entry for a series"""
    return {'id': pid, 'name': name}


# The series which the demo reviews
TARGET = 600214

# Patches which the upstream has, so that the inbox shows them as applied:
# (branch, subject)
APPLIED = [
    ('main', 'doc: Fix typos in the build guide'),
    ('next', 'arm: Add a device tree for the Demo board'),
    ('next', 'arm: Add a defconfig for the Demo board'),
]

# The state of some patches on patchwork, as patman last saw them
STATES = {9001: 'changes-requested', 9002: 'changes-requested'}

# Made-up series on patchwork, newest first: (id, name, version, hours ago,
#     (submitter name, email), cover-letter ID, patches)
SERIES = [
    (TARGET, 'video: Add a driver for the Ample AB1234 panel', 2, 2,
     ('Alex Ample', 'alex@example.com'), 9100, [
         _patch(9101, '[v2,1/3] video: Add a driver for the Ample AB1234 '
                'panel'),
         _patch(9102, '[v2,2/3] sandbox: Enable the Ample panel'),
         _patch(9103, '[v2,3/3] doc: Describe the Ample panel driver')]),
    (600207, 'boot: Allow a fallback when a bootflow fails', 1, 5,
     ('Sam Sample', 'sam@example.com'), 9000, [
         _patch(9001, '[1/2] boot: Record why a bootflow failed'),
         _patch(9002, '[2/2] boot: Try the next bootflow after a failure')]),
    (600199, 'spi: Tidy up the clock setup', 3, 9,
     ('Riley Example', 'riley@example.com'), None, [
         _patch(8901, '[v3] spi: Tidy up the clock setup')]),
    (600190, 'net: Add a driver for the Example SoC Ethernet', 4, 20,
     ('Jo Placeholder', 'jo@example.com'), 8800, [
         _patch(8801 + i, f'[v4,{i + 1}/4] net: {name}')
         for i, name in enumerate([
             'Add a driver for the Example SoC Ethernet',
             'Add a PHY reset to the Example driver',
             'Enable the Example Ethernet on the Demo board',
             'Add tests for the Example Ethernet driver'])]),
    (600185, 'doc: Fix typos in the build guide', 1, 26,
     ('Max Mock', 'max@example.com'), None, [
         _patch(8701, 'doc: Fix typos in the build guide')]),
    (600176, 'arm: Add support for the Demo board', 1, 30,
     ('Kim Fixture', 'kim@example.com'), 8600, [
         _patch(8601 + i, f'[{i + 1}/3] arm: {name}')
         for i, name in enumerate([
             'Add a device tree for the Demo board',
             'Add a defconfig for the Demo board',
             'Add the Demo board to the docs'])]),
]

COVER_TEXT = '''The Ample AB1234 is a small LCD panel which is used on the Demo
board. This series adds a driver for it, enables it on sandbox so that it
has some test coverage, and describes it in the docs.

Changes in v2:
- Use the reset GPIO from the device tree
- Add a sandbox test
'''

# The video Kconfig, before the first patch of the series adds the panel to it
KCONFIG = '''# SPDX-License-Identifier: GPL-2.0+
#
# Video drivers
#

menu "Graphics support"

config VIDEO
	bool "Enable driver model support for LCD and video"
	depends on DM
	help
	  This enables driver model for LCD and video devices, which show a
	  bitmap display, used for a console on the screen or a splash
	  screen.

config VIDEO_COPY
	bool "Keep a copy of the frame buffer"
	depends on VIDEO
	help
	  Draw into a copy of the frame buffer, which is quick to read, and
	  copy each change to the hardware frame buffer, which may be slow to
	  read.

config PANEL
	bool "Enable panel support"
	depends on VIDEO
	help
	  This adds a uclass for panels, which can turn their backlight on
	  and off.

config SIMPLE_PANEL
	bool "Enable a simple panel driver"
	depends on PANEL && DM_GPIO
	help
	  This adds a driver for a panel which needs only a GPIO to enable it
	  and a backlight.

endmenu
'''

# What the first patch adds to the Kconfig, at the end of the menu
KCONFIG_PANEL = '''
config VIDEO_AMPLE_AB1234
	bool "Enable the Ample AB1234 panel"
	depends on PANEL && DM_GPIO
'''

# The driver which the first patch adds
DRIVER = '''// SPDX-License-Identifier: GPL-2.0+
/*
 * Driver for the Ample AB1234 LCD panel
 */

#include <dm.h>
#include <panel.h>
#include <asm/gpio.h>

struct ample_priv {
	struct gpio_desc reset;
};

static int ample_enable_backlight(struct udevice *dev)
{
	struct ample_priv *priv = dev_get_priv(dev);

	dm_gpio_set_value(&priv->reset, 0);
	mdelay(120);

	return 0;
}

static int ample_probe(struct udevice *dev)
{
	struct ample_priv *priv = dev_get_priv(dev);

	gpio_request_by_name(dev, "reset-gpios", 0, &priv->reset,
			     GPIOD_IS_OUT);

	return 0;
}
'''

# The files before and after the first patch, by path. The demo's git repo
# holds both, so that the page can show the whole of each file, and the
# patch's diff comes from them
PATCH1_BEFORE = {'drivers/video/Kconfig': KCONFIG}
PATCH1_AFTER = {
    'drivers/video/Kconfig': KCONFIG.replace('\nendmenu\n',
                                             f'{KCONFIG_PANEL}\nendmenu\n'),
    'drivers/video/ample-ab1234.c': DRIVER,
}

# The commit message and diff of each patch, by patchwork ID. The diff of the
# first patch is filled in from the git repo
PATCH_TEXT = {
    9101: ('''Add a driver for the Ample AB1234 LCD panel, which is used on the
Demo board. It supports the panel's single 480x272 mode.

Signed-off-by: Alex Ample <alex@example.com>
''', None),
    9102: ('''Enable the panel on sandbox, so that the driver is built and tested.

Signed-off-by: Alex Ample <alex@example.com>
''', '''diff --git a/configs/sandbox_defconfig b/configs/sandbox_defconfig
--- a/configs/sandbox_defconfig
+++ b/configs/sandbox_defconfig
@@ -310,6 +310,7 @@ CONFIG_VIDEO=y
 CONFIG_VIDEO_COPY=y
+CONFIG_VIDEO_AMPLE_AB1234=y
'''),
    9103: ('''Add a short description of the driver and its device-tree binding.

Signed-off-by: Alex Ample <alex@example.com>
''', '''diff --git a/doc/develop/ample.rst b/doc/develop/ample.rst
new file mode 100644
--- /dev/null
+++ b/doc/develop/ample.rst
@@ -0,0 +1,5 @@
+Ample AB1234 panel
+==================
+
+The Ample AB1234 is a 480x272 panel. Its reset line is given by the
+``reset-gpios`` property.
'''),
}

# Reviews which the fake review job writes, by patch number
REVIEWS = {
    1: '''Hi Alex,

On {date}, Alex Ample <alex@example.com> wrote:
> video: Add a driver for the Ample AB1234 panel
>
> Add a driver for the Ample AB1234 LCD panel, which is used on the
> Demo board. It supports the panel's single 480x272 mode.

> diff --git a/drivers/video/ample-ab1234.c b/drivers/video/ample-ab1234.c
> @@ -0,0 +1,32 @@
> +	gpio_request_by_name(dev, "reset-gpios", 0, &priv->reset,
> +			     GPIOD_IS_OUT);

This ignores the return value, so a missing reset-gpios property is only
noticed later, when the backlight is enabled. Please check it and return the
error, e.g. with log_msg_ret()

> diff --git a/drivers/video/ample-ab1234.c b/drivers/video/ample-ab1234.c
> @@ -0,0 +1,32 @@
> +	dm_gpio_set_value(&priv->reset, 0);
> +	mdelay(120);

Where does the 120ms come from? Please add a comment pointing to the
datasheet, since it is a long delay.

> diff --git a/drivers/video/Kconfig b/drivers/video/Kconfig
> @@ -35,4 +35,8 @@ config SIMPLE_PANEL
> +config VIDEO_AMPLE_AB1234
> +	bool "Enable the Ample AB1234 panel"
> +	depends on PANEL && DM_GPIO

Please add some help, saying which boards have this panel

Regards,
Simon''',
    2: '''On {date}, Alex Ample <alex@example.com> wrote:
> sandbox: Enable the Ample panel
>
> Enable the panel on sandbox, so that the driver is built and tested.

Reviewed-by: Simon Glass <sjg@chromium.org>''',
    3: '''Hi Alex,

On {date}, Alex Ample <alex@example.com> wrote:
> doc: Describe the Ample panel driver

> diff --git a/doc/develop/ample.rst b/doc/develop/ample.rst
> @@ -0,0 +1,5 @@
> +The Ample AB1234 is a 480x272 panel. Its reset line is given by the
> +``reset-gpios`` property.

Please also mention that the panel needs the backlight enabled, as the
driver does in ample_enable_backlight()

Regards,
Simon''',
}

# How the fake 'Revise' job changes the review of patch 1: (text to replace,
# new text)
REVISION = ('''Where does the 120ms come from? Please add a comment pointing to the
datasheet, since it is a long delay.''',
            '''Where does the 120ms come from? Please add a comment pointing to the
datasheet. If the panel is ready sooner than that, a shorter delay would
speed up booting.''')

# Comments on patchwork, by (kind, ID)
COMMENTS = {
    ('patches', 9102): [{
        'id': 7001, 'hours': 1,
        'submitter': {'name': 'Tom Tester', 'email': 'tom@example.com'},
        'subject': 'Re: [PATCH v2 2/3] sandbox: Enable the Ample panel',
        'msgid': '<demo-7001@example.com>',
        'web_url': f'{PW_URL}/comment/7001/',
        'content': '''On Mon, Alex Ample wrote:
> Enable the panel on sandbox, so that the driver is built and tested.

Does this also need CONFIG_PANEL? The driver depends on it, so I don't
think it is built otherwise.

Tom
'''}],
}

# The response which the fake 'Respond' job suggests, by comment ID
COMMENT_RESPONSES = {
    7001: '''Hi Tom,

On {date}, Tom Tester <tom@example.com> wrote:
> Does this also need CONFIG_PANEL? The driver depends on it, so I don't
> think it is built otherwise.

Good catch, it does. Alex, please add CONFIG_PANEL=y here too.

Regards,
Simon''',
}

# A reply to the earlier review of the Ethernet driver, and the response
# which the fake 'Suggest a response' job writes
REPLY = '''Hi Simon,

On Tue, Simon Glass <sjg@chromium.org> wrote:
> Please split this up.

Would it be OK to split out the PHY reset and the tests, and keep the rest
together? The driver does not build without its register definitions.

Thanks,
Jo
'''

REPLY_RESPONSE = '''Hi Jo,

On {date}, Jo Placeholder <jo@example.com> wrote:
> Would it be OK to split out the PHY reset and the tests, and keep the rest
> together? The driver does not build without its register definitions.

Yes, that is fine. Please also put the board change in its own patch.

Regards,
Simon'''


# The stand-in for 'python -m patman' in jobs, which runs run_job() with the
# real patman, rather than itself
FAKE_MAIN = '''import os
import sys

fake = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path = [path for path in sys.path if os.path.abspath(path or '.') != fake]
for name in [name for name in sys.modules if name.split('.')[0] == 'patman']:
    del sys.modules[name]
sys.path.insert(0, os.environ['PATMAN_DEMO_DIR'])
import serve_demo
sys.exit(serve_demo.run_job(os.environ['PATMAN_DEMO_DB'], sys.argv[1:]))
'''


def _fake_patchwork(subpath):
    """Answer a request to the fake patchwork server

    Args:
        subpath (str): Part of the URL after /api/1.2/

    Return:
        dict or list: Response
    """
    if subpath.startswith('series/?'):
        page = '&page=1&' in subpath
        return [{'id': sid, 'name': name, 'version': ver, 'date': _ago(hours),
                 'submitter': {'name': who[0], 'email': who[1]},
                 'total': len(patches), 'received_all': True,
                 'web_url': f'{PW_URL}/project/uboot/list/?series={sid}',
                 'cover_letter': ({'id': cover, 'name': f'[v{ver},0/'
                                   f'{len(patches)}] {name}'}
                                  if cover else None),
                 'patches': patches}
                for sid, name, ver, hours, who, cover, patches in SERIES
                if page]
    if subpath.startswith('events/'):
        return []
    if subpath.endswith('/comments/'):
        kind, item_id = subpath.split('/')[:2]
        return [{**com, 'date': _ago(com['hours'])}
                for com in COMMENTS.get((kind, int(item_id)), [])]
    if subpath.startswith('patches/'):
        pid = int(subpath.split('/')[1])
        content, diff = PATCH_TEXT.get(pid, ('', ''))
        return {'id': pid, 'name': _patch_name(pid), 'date': _ago(2),
                'submitter': {'name': 'Alex Ample',
                              'email': 'alex@example.com'},
                'state': 'new', 'content': content, 'diff': diff,
                'web_url': f'{PW_URL}/patch/{pid}/',
                'series': [{'id': TARGET, 'name': SERIES[0][1],
                            'version': 2}]}
    if subpath.startswith('covers/'):
        cid = int(subpath.split('/')[1])
        return {'id': cid, 'name': f'[v2,0/3] {SERIES[0][1]}',
                'date': _ago(2), 'content': COVER_TEXT,
                'submitter': {'name': 'Alex Ample',
                              'email': 'alex@example.com'},
                'web_url': f'{PW_URL}/cover/{cid}/'}
    raise ValueError(f"Unexpected request '{subpath}'")


def _patch_name(pid):
    """Get the name of a patch, from the made-up series"""
    for _, _, _, _, _, _, patches in SERIES:
        for pat in patches:
            if pat['id'] == pid:
                return pat['name']
    return ''


def _add_review(db, link, desc, version, patches, reviews, sent, hours=0):
    """Add a reviewed series to the database

    Args:
        db (Database): Open database
        link (int): Patchwork series ID
        desc (str): Name of the series
        version (int): Version
        patches (list of tuple): (subject, patchwork ID) for each patch
        reviews (dict): Body of each review, by patch number
        sent (bool): True if the reviews have been sent, else they are
            drafts
        hours (float): How many hours ago the reviews were written

    Return:
        int: ID of the version in the database
    """
    found = db.series_find_by_link(str(link))
    if found:
        series_id = found[0]
    else:
        series_id = None
        for ser in db.series_get_dict(reviews_only=True).values():
            if ser.desc == desc:
                series_id = ser.idnum
        if not series_id:
            series_id = db.series_add(f'pw-{link}-review', desc, UPSTREAM)
            db.series_set_source(series_id, 'review')
    svid = db.ser_ver_add(series_id, version, link=str(link))
    db.pcommit_add_list(svid, [Pcommit(None, seq, subject, None, None, None,
                                       None, None)
                               for seq, (subject, _) in enumerate(patches)])
    for pcm, (_, pid) in zip(db.pcommit_get_list(svid), patches):
        db.pcommit_update(Pcommit(pcm.idnum, None, None, None, None, 'new',
                                  pid, 0))
    stamp = (datetime.now() - timedelta(hours=hours)).isoformat()
    for seq, body in reviews.items():
        rid = db.review_add(svid, seq, body, 'Reviewed-by:' in body, stamp)
        db.review_set_draft_id(rid, f'demo-draft-{rid}')
        if sent:
            db.review_set_sent(rid, body, f'demo-msg-{rid}',
                               f'demo-thread-{rid}')
    return svid


def _setup_upstream(topdir):
    """Create the upstream's git repo, with some of the series applied

    The inbox fetches it, as it would a real upstream, and finds the patches
    in APPLIED on its branches

    Args:
        topdir (str): Directory which holds the demo's git repo
    """
    upstream = os.path.join(topdir, 'upstream')

    def git(*args):
        subprocess.run(['git', '-c', 'user.name=Demo', '-c',
                        'user.email=demo@example.com', *args], cwd=upstream,
                       check=True, capture_output=True)

    os.mkdir(upstream)
    git('init', '-q', '-b', 'main')
    git('commit', '-q', '--allow-empty', '-m', 'Prepare the release')
    for branch, subject in APPLIED:
        if branch == 'main':
            git('commit', '-q', '--allow-empty', '-m', subject)
    git('checkout', '-q', '-b', 'next')
    for branch, subject in APPLIED:
        if branch == 'next':
            git('commit', '-q', '--allow-empty', '-m', subject)
    subprocess.run(['git', 'remote', 'add', UPSTREAM, upstream], cwd=topdir,
                   check=True)


def _setup_patch(topdir):
    """Put the files of the first patch, before and after it, in the git repo

    The commits are left out of the branches, but their files can be read
    from the repo, as the page does to show the whole of a file. The diff of
    the patch is made from them, so that it names their blobs

    Args:
        topdir (str): Directory which holds the demo's git repo
    """
    work = tempfile.mkdtemp(prefix='patman-demo-files-')
    env = {**os.environ, 'GIT_DIR': os.path.join(topdir, '.git'),
           'GIT_WORK_TREE': work,
           'GIT_INDEX_FILE': os.path.join(topdir, '.git', 'demo-index')}

    def git(*args):
        return subprocess.run(
            ['git', '-c', 'user.name=Demo', '-c', 'user.email=demo@example.com',
             '-c', 'diff.noprefix=false', '-c', 'diff.mnemonicPrefix=false',
             *args], env=env, cwd=work, check=True, capture_output=True,
            text=True).stdout.strip()

    def commit(files, *parents):
        for path, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(work, path)),
                        exist_ok=True)
            with open(os.path.join(work, path), 'w', encoding='utf-8') as outf:
                outf.write(text)
        git('add', '-A')
        tree = git('write-tree')
        return git('commit-tree', tree, '-m', 'Demo', *[
            arg for par in parents for arg in ('-p', par)])

    try:
        base = commit(PATCH1_BEFORE)
        patched = commit(PATCH1_AFTER, base)
        git('update-ref', 'refs/demo/patch1', patched)
        diff = git('diff', '--no-color', '--no-ext-diff', '--abbrev=11',
                   base, patched) + '\n'
    finally:
        shutil.rmtree(work, ignore_errors=True)

    PATCH_TEXT[9101] = (PATCH_TEXT[9101][0], diff)

    # Check that the chunks which the reviews quote are in the patches
    patches = SERIES[0][6]
    for seq, body in REVIEWS.items():
        diff = PATCH_TEXT[patches[seq - 1]['id']][1]
        for line in body.splitlines():
            if line.startswith('> @@') and f'\n{line[2:]}\n' not in diff:
                raise ValueError(f"The review of patch {seq} quotes "
                                 f"'{line[2:]}', which is not in its diff")


def setup(topdir):
    """Create the database, with an upstream and some earlier reviews

    Args:
        topdir (str): Directory to hold the git repo and database
    """
    subprocess.run(['git', 'init', '-q', topdir], check=True)
    _setup_upstream(topdir)
    _setup_patch(topdir)
    db = Database(os.path.join(topdir, '.patman.db'))
    db.start()
    db.upstream_add(UPSTREAM, 'https://source.example.org/u-boot.git',
                    PW_URL)
    db.upstream_set_default(UPSTREAM)
    db.patchwork_update('U-Boot', 1, 'uboot', UPSTREAM)

    # Earlier reviews, so that the lists are not empty
    _add_review(db, 600199, 'spi: Tidy up the clock setup', 3,
                [('spi: Tidy up the clock setup', 8901)],
                {1: 'Reviewed-by: Simon Glass <sjg@chromium.org>'}, True, 6)
    svid = _add_review(
        db, 600112, 'net: Add a driver for the Example SoC Ethernet', 3,
        [('net: Add a driver for the Example SoC Ethernet', 8501)],
        {1: 'Hi Jo,\n\nPlease split this up.\n\nRegards,\nSimon'}, True,
        4 * 24)

    # Jo replies to that review
    rid = db.review_get_for_version(svid)[0].idnum
    when = datetime.now(timezone.utc) - timedelta(days=2)
    db.reply_add(rid, 'demo-reply-1', '<demo-reply-1@example.com>', '',
                 'Jo Placeholder <jo@example.com>', 'sjg@chromium.org', '',
                 'Re: [PATCH v3] net: Add a driver for the Example SoC '
                 'Ethernet', email.utils.format_datetime(when), REPLY,
                 when.isoformat())
    db.review_set_replied(rid)
    _add_review(db, 600054, 'i2c: Support clock stretching', 1,
                [('i2c: Support clock stretching', 8101)],
                {1: 'Reviewed-by: Simon Glass <sjg@chromium.org>'}, True,
                9 * 24)
    db.commit()
    db.close()


def _say(*lines, delay=0.6):
    """Print some output from a fake job, a line at a time"""
    for line in lines:
        print(line, flush=True)
        time.sleep(delay)


def run_job(db_path, args):
    """Pretend to be 'patman review', for a job started by the page

    Args:
        db_path (str): Path to the database
        args (list of str): Arguments which would be given to patman

    Return:
        int: Exit code
    """
    args = [arg for arg in args if arg != '-v']
    link = args[args.index('-s') + 1] if '-s' in args else str(TARGET)
    seqs = ([int(seq) for seq in args[args.index('-i') + 1].split(',')]
            if '-i' in args else None)
    db = Database(db_path)
    db.open_it()
    try:
        for action in ('--suggest-reply', '--draft-reply', '--send-reply'):
            if action in args:
                return _reply_job(db, action,
                                  int(args[args.index(action) + 1]))
        if '--respond-comment' in args:
            return _respond_job(
                db, link, seqs[0],
                int(args[args.index('--respond-comment') + 1]))
        if '--revise' in args:
            _say('Asking the agent to revise the reviews...', delay=2)
            svid = db.series_find_by_link(link)[3]
            for rev in db.review_get_for_version(svid):
                if (rev.status in ('draft', 'edited') and
                        (not seqs or rev.seq in seqs) and
                        REVISION[0] in rev.body):
                    db.review_set_body(rev.idnum,
                                       rev.body.replace(*REVISION),
                                       rev.approved)
                    if '-d' in args:
                        db.review_set_draft_id(rev.idnum,
                                               f'demo-draft-{rev.idnum}-3')
                    _say(f'Revised the review of patch {rev.seq}')
            db.commit()
            return 0
        if '--delete' in args:
            svid = db.series_find_by_link(link)[3]
            for rev in db.review_get_for_version(svid):
                if rev.status in ('draft', 'edited') and rev.seq in seqs:
                    db.review_delete(rev.idnum)
                    _say(f'Deleted the review of patch {rev.seq}')
            db.commit()
            return 0
        if seqs == [0]:
            _say('Reviewing the cover letter...', delay=2)
            _say('The agent has no comments on the cover letter')
            return 0
        if '--send' in args:
            _say(f"Sending the reviews of series {link}")
            svid = db.series_find_by_link(link)[3]
            for rev in db.review_get_for_version(svid):
                if rev.status in ('draft', 'edited') and \
                        (not seqs or rev.seq in seqs):
                    db.review_set_sent(rev.idnum, rev.body,
                                       f'demo-msg-{rev.idnum}',
                                       f'demo-thread-{rev.idnum}')
                    db.commit()
                    _say(f'Sent the review of patch {rev.seq}')
            return 0
        if '--redraft' in args:
            svid = db.series_find_by_link(link)[3]
            done = 0
            for rev in db.review_get_for_version(svid):
                if not seqs or rev.seq in seqs:
                    db.review_set_draft_id(rev.idnum,
                                           f'demo-draft-{rev.idnum}-2')
                    done += 1
            db.commit()
            _say(f'Created {done} Gmail draft(s) (link {link})')
            return 0

        # A review of the series
        _, name, version, _, _, cover, patches = next(
            item for item in SERIES if str(item[0]) == link)
        subjects = [pat['name'].split('] ', 1)[-1] for pat in patches]
        _say(f"Series: {name}", f'Version: {version}, Patches: {len(patches)}',
             f"Added series '{name}' v{version} to database",
             f'Applying series to branch {UPSTREAM}-{link}-review...',
             f'Applied {len(patches)} patches', 'Building sandbox... ok',
             delay=0.8)
        for seq, subject in enumerate(subjects, 1):
            _say(f'Reviewing patch {seq}/{len(patches)}: {subject}',
                 delay=1.5)
        reviews = {seq: body.format(date=_ago(2)[:10])
                   for seq, body in REVIEWS.items()}
        svid = _add_review(db, link, name, version,
                           list(zip(subjects, [pat['id'] for pat in patches])),
                           reviews, False)
        db.ser_ver_set_info(SerVer(svid, None, None, None, cover, 0,
                                   f'[v{version},0/{len(patches)}] {name}',
                                   None, None, None))
        db.commit()
        _say(f'Created {len(reviews)} Gmail draft(s) (link {link})')
        return 0
    finally:
        db.close()


def _reply_job(db, action, reply_id):
    """Pretend to suggest, draft or send a response to a reply

    Args:
        db (Database): Open database
        action (str): '--suggest-reply', '--draft-reply' or '--send-reply'
        reply_id (int): ID of the reply record

    Return:
        int: Exit code
    """
    if action == '--suggest-reply':
        _say('Asking the agent for a response...', delay=2)
        when = email.utils.parsedate_to_datetime(db.reply_get(reply_id).date)
        db.reply_set_response(
            reply_id, REPLY_RESPONSE.format(date=when.strftime('%Y-%m-%d')),
            'suggested')
        _say('Suggested a response')
    elif action == '--draft-reply':
        db.reply_set_status(reply_id, 'draft', f'demo-draft-reply-{reply_id}')
        _say('Created a Gmail draft of the response')
    else:
        db.reply_set_status(reply_id, 'sent')
        _say('Sent the response')
    db.commit()
    return 0


def _respond_job(db, link, seq, comment_id):
    """Pretend to have the agent suggest a response to a patchwork comment

    Args:
        db (Database): Open database
        link (str): Patchwork series ID
        seq (int): Patch which the comment is about, 0 for the cover letter
        comment_id (int): ID of the comment on patchwork

    Return:
        int: Exit code
    """
    com = next(com for coms in COMMENTS.values() for com in coms
               if com['id'] == comment_id)
    _say(f"Reading comment {comment_id} from patchwork",
         'Asking the agent for a response...', delay=1.5)
    when = datetime.now(timezone.utc) - timedelta(hours=com['hours'])
    date = email.utils.format_datetime(when)
    who = com['submitter']
    reply_id = db.reply_add_comment(
        db.series_find_by_link(link)[3], seq, comment_id, com['msgid'], '',
        f"{who['name']} <{who['email']}>", '', '', com['subject'], date,
        com['content'], datetime.now().isoformat())
    db.reply_set_response(
        reply_id, COMMENT_RESPONSES[comment_id].format(
            date=when.strftime('%Y-%m-%d')), 'suggested')
    db.commit()
    _say('Suggested a response')
    return 0


def serve(port):
    """Run the server on a throwaway database

    Args:
        port (int): Port to listen on
    """
    topdir = tempfile.mkdtemp(prefix='patman-demo-')
    try:
        _serve(topdir, port)
    finally:
        shutil.rmtree(topdir, ignore_errors=True)


def _serve(topdir, port):
    """Set up the database and run the server until it is stopped

    Args:
        topdir (str): Empty directory to hold the git repo and database
        port (int): Port to listen on
    """
    # pylint: disable=C0415
    from aiohttp import web
    from patman import cseries
    from patman.review import scan
    from patman.server.app import Server

    setup(topdir)
    cser = cseries.Cseries(topdir)
    cser.open_database()
    srv = Server(cser, pw_url=PW_URL)
    pwork = Patchwork.for_testing(_fake_patchwork)
    pwork.url = PW_URL
    # A cache of its own, which already follows the project's events, so
    # that the inbox shows the states in STATES
    pwork.cache = pwcache.Cache(os.path.join(topdir, 'patchwork.db'))
    now = pwork.cache.now()
    pwork.cache.set_follow(PW_URL, 'uboot', now, '', now)
    pwork.cache.set_states(PW_URL, STATES)
    srv.pwork_for_testing = pwork
    # Jobs run 'python -m patman', which finds the stand-in package first
    fake = os.path.join(topdir, 'fake')
    os.makedirs(os.path.join(fake, 'patman'))
    with open(os.path.join(fake, 'patman', '__init__.py'), 'w',
              encoding='utf-8'):
        pass
    with open(os.path.join(fake, 'patman', '__main__.py'), 'w',
              encoding='utf-8') as outf:
        outf.write(FAKE_MAIN)
    real_env = scan.child_env

    def child_env():
        env = real_env()
        env['PYTHONPATH'] = fake + os.pathsep + env['PYTHONPATH']
        return env
    scan.child_env = child_env
    os.environ['PATMAN_DEMO_DIR'] = HERE
    os.environ['PATMAN_DEMO_DB'] = os.path.join(topdir, '.patman.db')
    print(f'Serving the demo on http://127.0.0.1:{port}/', flush=True)
    web.run_app(srv.make_app(), host='127.0.0.1', port=port, print=None)


def main():
    """Run the server, or a fake job"""
    if len(sys.argv) > 2 and sys.argv[1] == 'job':
        sys.exit(run_job(sys.argv[2], sys.argv[3:]))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, default=8050)
    args = parser.parse_args()
    serve(args.port)


if __name__ == '__main__':
    main()
