# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Suggest series to review, by your own rules

The rules, from the [suggest] section of the settings (see suggest_rules),
are run on the recent series, the cheap tests first: those which need only
the list of series on every series, then those which need the patches, or
extra requests or git, only on the series which could still be suggested.
"""

import asyncio
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from types import SimpleNamespace

import aiohttp

from u_boot_pylib import gitutil
from u_boot_pylib import tout

from patman import lorelist
from patman import pwcache
from patman import settings
from patman.review import apply
from patman.review import common
from patman.review import history
from patman.review import lookup
from patman.review import maintainers
from patman.review import patchdata
from patman.review import scan
from patman.review import suggest_rules
from patman.review import suggest_tests

# Oldest copy of the first page of recent series to use from the cache, in
# seconds; suggestions need not be up to the minute, so the pages which the
# inbox read in the last hour do, along with any later pages read with them
SUGGEST_MAX_AGE = 60 * 60

# What reading a series' patches or its submitter's earlier series can
# raise, e.g. when patchwork is busy, so that the series is left out
PW_ERRORS = (ValueError, aiohttp.ClientError, asyncio.TimeoutError)

# How long what a test finds on a series is kept in the cache, in seconds,
# for the tests whose result can change. The others look only at the
# patches, which do not change, so their results are kept until the cache
# drops them
HOUR = 60 * 60
RESULT_KEEP = {
    # Tags are collected from replies, CI runs and delegates change
    'has': HOUR, 'no-replies': HOUR, 'delegate': HOUR, 'checks': HOUR,
    # These look at your repo, which changes slowly
    'maintainer': 24 * HOUR, 'fixes': 24 * HOUR, 'blame': 24 * HOUR,
    'newcomer': 24 * HOUR,
}

# Tests which look at your repo, so whose results depend on which it is
REPO_TESTS = {'maintainer', 'fixes', 'blame', 'newcomer'}


# A series to suggest: the series ('pws', as from patchwork or lore), its
# score and the reasons for it, e.g. ['i2c +3']
Suggestion = namedtuple('Suggestion', 'pws,score,reasons')


def _newest(series):
    """Keep only the newest version of each series

    Args:
        series (list of dict): Series, as from patchwork or lore

    Returns:
        list of dict: The newest version of each, in the same order
    """
    newest = {}
    for pws in series:
        key = lookup.clean_series_name(pws.get('name') or '').lower()
        if int(pws.get('version') or 1) > int(
                (newest.get(key) or {}).get('version') or 0):
            newest[key] = pws
    keep = {id(pws) for pws in newest.values()}
    return [pws for pws in series if id(pws) in keep]


def _reviewed(pws, cser, by_key):
    """Check what you have reviewed of a series

    Args:
        pws (dict): Series, as from patchwork or lore
        cser (Cseries): Open cseries instance with the reviews
        by_key (dict): Series ID for each b4 key, from lookup.b4_series()

    Returns:
        tuple:
            bool: True if you have reviewed this version
            bool: True if you have reviewed an earlier version
            int or None: ID of the review series of the earlier version,
                if any
    """
    version = int(pws.get('version') or 1)
    existing = cser.db.series_find_by_link(str(pws['id']))
    if existing and cser.db.review_get_for_version(existing[3]):
        return True, False, None
    earlier = scan.find_reviewed(cser, pws, by_key)
    return False, bool(earlier) and (
        cser.db.series_get_max_version(earlier) or 0) < version, earlier


# For a 'similar' rule with no range, a series must share its prefix or
# submitter with at least SIMILAR_MIN of the series you reviewed, or with one
# in SIMILAR_SHARE of them if that is more, so that the more you review, the
# closer it must be
SIMILAR_MIN = 2
SIMILAR_SHARE = 100

# What you reviewed before, for the 'similar' test:
#   prefixes (dict): key (str): Prefix of a subject, e.g. 'dm: core:'
#       value (set of int): IDs of the review series with it
#   people (dict): key (str): Address of a submitter, in lower case
#       value (set of int): IDs of the review series they sent
#   total (int): Number of series you reviewed
Profile = namedtuple('Profile', 'prefixes,people,total')


def read_profile(db, series):
    """Find the prefixes and submitters of the series you reviewed

    The submitter of a series comes from the headers of your reviews of it,
    else from the recent series

    Args:
        db (Database): Database with the reviews
        series (list of dict): Recent series, as from patchwork or lore

    Returns:
        Profile: What you reviewed
    """
    prefixes = {}
    people = {}
    reviewed = set()
    sent_by = {str(pws['id']): ((pws.get('submitter') or {}).get('email') or
                                '').lower() for pws in series}
    for series_id, link, subject in db.review_get_subjects():
        reviewed.add(series_id)
        prefix = suggest_tests.prefix_of(
            suggest_tests.RE_TAGS.sub('', subject or '').strip())
        if prefix:
            prefixes.setdefault(prefix, set()).add(series_id)
        if sent_by.get(str(link)):
            people.setdefault(sent_by[str(link)], set()).add(series_id)
    for series_id, addrs in db.review_get_recipients().items():
        for addr in addrs:
            people.setdefault(addr, set()).add(series_id)
        reviewed.add(series_id)
    return Profile(prefixes, people, len(reviewed))


def _like(profile, info, earlier):
    """Find what a series shares with the series you reviewed before

    Args:
        profile (Profile): What you reviewed
        info (suggest_tests.Info): The series
        earlier (int or None): ID of the review series of an earlier version
            of it, which is left out

    Returns:
        list of suggest_tests.Like: What it shares, those shared with the
            most series first
    """
    shared = [('prefix', prefix, profile.prefixes.get(prefix, set()))
              for prefix in dict.fromkeys(filter(None, map(
                  suggest_tests.prefix_of, info.subjects)))]
    shared.append(('from', info.email, profile.people.get(info.email, set())))
    found = [suggest_tests.Like(kind, what, len(ids - {earlier}))
             for kind, what, ids in shared if ids - {earlier}]
    return sorted(found, key=lambda like: -like.count)


# A series which the free tests leave in the running: the series ('pws'),
# its score and reasons so far, and the best score it could reach
Candidate = namedtuple('Candidate', 'pws,score,reasons,best')


def _gain(rules):
    """Get the most which some rules can add to a score

    Args:
        rules (list of Rule): Rules

    Returns:
        int: Total of their positive scores
    """
    return sum(rule.score for rule in rules
               if isinstance(rule.score, int) and rule.score > 0)


class Suggester:  # pylint: disable=R0902
    """Finds the series to suggest, running the cheap tests first

    The free tests are run on every recent series. The series which could
    still reach min_score are then taken in order of the best score they
    could reach, and their patches read for the tests which need them, then
    the tests which need extra requests or git are run, stopping once no
    series left could get into the top 'limit'. Once max_reads series have
    been read, a series whose patches are needed is left out

    What the costly tests find on each series is kept in the patchwork
    cache (see pwcache), for the time given by RESULT_KEEP, so that a
    series is not read or tested again each time

    Properties:
        rules (Rules): Rules to use
        pwork (Patchwork or LoreList): Where the series come from
        cser (Cseries): Open cseries instance with the reviews
        emails (set of str): Your addresses, in lower case
        own_db (Database): Database holding your own series
        now (datetime): The time now, with its time zone
        limit (int): Most series to suggest
        upstream (str or None): Branch to use for the history of the tree,
            for a series with no base-commit which is in the repo
        reads (int): Number of series whose patches were read
        complete (bool): True if every series in the running was scored,
            False if some were left since they could not get into the top
            'limit', or max_reads was reached
        sections (list of Section or None): MAINTAINERS sections, once read
        series (list of dict): The recent series, once candidates() has run
    """
    # pylint: disable-next=R0913
    def __init__(self, rules, pwork, cser, emails, own_db, now, limit,
                 upstream=None):
        self.rules = rules
        self.pwork = pwork
        self.cser = cser
        self.emails = emails
        self.own_db = own_db
        self.now = now
        self.limit = limit
        self.upstream = upstream
        self.reads = 0
        self.complete = True
        self.sections = None
        self.series = []
        self._top = None
        self._authors = None
        self._keys = None
        self._rule_keys = {}
        self._capped = False
        self._profile = None

    def _rules(self, tests):
        """Get the rules whose tests are in a set

        Args:
            tests (dict): Tests, e.g. suggest_tests.FREE_TESTS

        Returns:
            list of Rule: The rules
        """
        return [rule for rule in self.rules.rules if rule.test in tests]

    def _uses(self, test):
        """Check whether a rule uses a test

        Args:
            test (str): Test, e.g. 'blame'

        Returns:
            bool: True if one does
        """
        return any(rule.test == test for rule in self.rules.rules)

    def needs_patches(self):
        """Check whether the rules need the patches of a series

        Returns:
            bool: True if they do
        """
        return (bool(self._rules(suggest_tests.PATCH_TESTS)) or
                self._uses('blame'))

    def git_dir(self):
        """Get the top of the git repo, for the tests which need it

        Returns:
            str: Directory
        """
        if self._top is None:
            self._top = gitutil.get_top_level() or '.'
        return self._top

    def free_info(self, pws):
        """Get what the free tests know about a series

        Args:
            pws (dict): Series, as from patchwork or lore

        Returns:
            tuple:
                bool: True if you have reviewed this version
                suggest_tests.Info: What is known
        """
        done, before, earlier = _reviewed(pws, self.cser, self._by_key())
        states = self.pwork.get_states([pat['id'] for pat in
                                        pws.get('patches') or []])
        info = suggest_tests.series_info(pws, self.now, before,
                                         list(states.values()))
        if self._uses('similar'):
            if self._profile is None:
                self._profile = read_profile(self.cser.db, self.series)
            info = info._replace(
                like=_like(self._profile, info, earlier),
                like_min=max(SIMILAR_MIN,
                             self._profile.total // SIMILAR_SHARE))
        return done, info

    def _by_key(self):
        """Get the series ID for each b4 key, reading them the first time

        Returns:
            dict: As from lookup.b4_series()
        """
        if self._keys is None:
            self._keys = lookup.b4_series(self.cser.db)
        return self._keys

    def candidates(self, series):
        """Run the free tests on recent series

        Your own series are left out, as are those you have already reviewed
        at this version, older versions of a series and those which cannot
        reach min_score

        Args:
            series (list of dict): Recent series, as from patchwork or lore

        Returns:
            list of Candidate: Series still in the running, those which
                could reach the highest score first, then the newest
        """
        self.series = series
        free = self._rules(suggest_tests.FREE_TESTS)
        gain = _gain(self._rules(suggest_tests.PATCH_TESTS) +
                     self._rules(suggest_tests.EXTRA_TESTS))
        found = []
        for pws in _newest(series):
            if lookup.is_own_series(pws, self.emails, self.own_db):
                continue
            done, info = self.free_info(pws)
            if done:
                continue
            result = suggest_tests.score_series(
                free, info, suggest_tests.FREE_TESTS)
            if result and result[0] + gain >= self.rules.min_score:
                found.append(Candidate(pws, *result, result[0] + gain))
        found.sort(key=lambda cand: (cand.best, cand.pws.get('date') or ''),
                   reverse=True)
        return found

    async def patch_info(self, client, pws):
        """Read the patches of a series and find what the tests need

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore

        Returns:
            patchdata.PatchInfo: What is known

        Raises:
            ValueError: The patches cannot be read
        """
        if self.sections is None and self._uses('maintainer'):
            self.sections = maintainers.read(self.git_dir())
        self.reads += 1
        mbox = await self.pwork.get_series_mbox(client, str(pws['id']))
        info = patchdata.patch_info(mbox, self.emails, self.sections)
        if info.fixes and self._uses('fixes'):
            info = info._replace(own_fixes=history.own_commits(
                self.git_dir(), info.fixes, self.emails))
        return info

    async def _newcomer(self, client, pws):
        """Check whether the submitter of a series has sent one before

        Earlier versions of the series do not count. The recent series and
        the history of the tree are looked at first, since they need no
        request

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore

        Returns:
            tuple:
                bool: True if the submitter is new
                str: How that was decided

        Raises:
            ValueError: The earlier series cannot be read
        """
        addr = ((pws.get('submitter') or {}).get('email') or '').lower()
        if not addr:
            return False, 'no address'
        name = lookup.clean_series_name(pws.get('name') or '').lower()
        date = pws.get('date') or ''

        def _other(ser):
            return lookup.clean_series_name(
                ser.get('name') or '').lower() != name

        for ser in self.series:
            if (((ser.get('submitter') or {}).get('email') or '').lower() ==
                    addr and (ser.get('date') or '') < date and _other(ser)):
                return False, f"sent '{ser.get('name')}'"
        if self._authors is None:
            self._authors = history.authors(self.git_dir(),
                                            self.upstream or 'HEAD')
        if addr in self._authors:
            return False, 'has commits in the tree'
        earlier = await self.pwork.earlier_series(
            client, self.pwork.link_name, addr, date)
        earlier = [ser for ser in earlier if _other(ser)]
        if earlier:
            return False, f"sent '{earlier[0].get('name')}'"
        return True, 'no earlier series'

    async def _replier(self, client, pws, age):
        """Find who, other than the submitter, has replied to a series

        The cover letter is looked at first, then each patch in turn,
        stopping at the first reply found. Nothing is read if no
        'no-replies' rule wants a series of this age

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore
            age (int): Whole days since it was sent

        Returns:
            str or None: Name of the first person found, '' if nobody, or
                None if not looked at

        Raises:
            ValueError: The replies cannot be read
        """
        if not any(suggest_tests.in_range(age, rule.args)
                   for rule in self.rules.rules if rule.test == 'no-replies'):
            return None
        addr = ((pws.get('submitter') or {}).get('email') or '').lower()
        reads = []
        cover = (pws.get('cover_letter') or {}).get('id')
        if cover:
            reads.append((self.pwork.get_cover_comments, cover))
        reads += [(self.pwork.get_patch_comments, pat['id'])
                  for pat in pws.get('patches') or []]
        for read, item in reads:
            for comment in await read(client, item):
                who = comment.get('submitter') or {}
                if (who.get('email') or '').lower() != addr:
                    return who.get('name') or who.get('email') or 'someone'
        return ''

    async def _patch_list(self, client, pws):
        """Find the delegates and CI results of the patches of a series

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore

        Returns:
            tuple:
                list of str or None: Delegate of each patch which has one,
                    or None if patchwork does not say, e.g. on lore
                bool: True if a patch is delegated to you
                list of str or None: CI result of each patch, or None

        Raises:
            ValueError: The patches cannot be read
        """
        read = getattr(self.pwork, 'get_series_patches', None)
        if not read:
            return None, False, None
        patches = await read(client, str(pws['id']))
        people = [pat['delegate'] for pat in patches if pat.get('delegate')]
        mine = any((who.get('email') or '').lower() in self.emails
                   for who in people)
        return ([who.get('username') or who.get('email') or '?'
                 for who in people], mine,
                [pat.get('check') or 'pending' for pat in patches])

    # pylint: disable-next=R0914
    async def extra(self, client, pws, info, tests=None):
        """Find what the tests which need extra requests or git need

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore
            info (patchdata.PatchInfo or None): Its patches, if read
            tests (set of str or None): Tests to find out for, or None for
                all those which the rules use

        Returns:
            suggest_tests.Extra: What is known

        Raises:
            ValueError: Something which is needed cannot be read
        """
        uses = self._uses if tests is None else tests.__contains__
        blame = {}
        if info and info.removed and uses('blame'):
            top = self.git_dir()
            ref = (info.base if info.base and history.ref_exists(
                top, info.base) else self.upstream)
            if ref:
                blame = history.blame_mine(top, ref, info.removed,
                                           self.emails)
        newcomer, how = False, ''
        if uses('newcomer'):
            newcomer, how = await self._newcomer(client, pws)
        age = suggest_tests.age_days(pws.get('date'), self.now)
        replier = None
        if uses('no-replies'):
            replier = await self._replier(client, pws, age)
        delegates, mine, checks = None, False, None
        if uses('delegate') or uses('checks'):
            delegates, mine, checks = await self._patch_list(client, pws)
        return suggest_tests.Extra(blame, newcomer, how, age, replier,
                                   delegates, mine, checks)

    def _could_place(self, found, best):
        """Check whether a series could get into the top 'limit'

        Args:
            found (list of Suggestion): Series found so far
            best (int): Best score the series could reach

        Returns:
            bool: True if it could
        """
        scores = sorted((sug.score for sug in found), reverse=True)
        return len(scores) < self.limit or best >= scores[self.limit - 1]

    def _key(self, rule):
        """Get the key of what the test of a rule finds, for the cache

        The key covers what the result depends on, besides the series: the
        test and its arguments, your addresses and, for a test which looks
        at your repo, the repo and its upstream. It leaves out the rule's
        name, score and 'not', which are applied to the result

        Args:
            rule (Rule): Rule

        Returns:
            str: Key
        """
        if rule.name not in self._rule_keys:
            parts = [suggest_rules.rule_text(rule._replace(negate=False)),
                     sorted(self.emails)]
            if rule.test in REPO_TESTS:
                parts += [os.path.abspath(self.git_dir()), self.upstream]
            self._rule_keys[rule.name] = hashlib.sha1(
                json.dumps(parts).encode('utf-8')).hexdigest()
        return self._rule_keys[rule.name]

    def _cached(self, pws):
        """Get what the costly tests found on a series before, if recent

        Args:
            pws (dict): Series, as from patchwork or lore

        Returns:
            dict: key (str): Key of each result, from _key()
                value (suggest_tests.Result): What the test found
        """
        cache = pwcache.get()
        if not cache:
            return {}
        keys = {self._key(rule): RESULT_KEEP.get(rule.test)
                for rule in self._rules(suggest_tests.PATCH_TESTS) +
                self._rules(suggest_tests.EXTRA_TESTS)}
        return {key: suggest_tests.Result(*res)
                for key, res in cache.get_results(
                    self.pwork.url, str(pws['id']), keys).items()}

    # pylint: disable-next=R0913
    def _apply(self, pws, rules, cached, known, tests):
        """Score a series by some rules, running the tests not in the cache

        What the tests find is stored in the cache

        Args:
            pws (dict): Series, as from patchwork or lore
            rules (list of Rule): Rules to use, each with a test in 'tests'
            cached (dict): What the tests found before, from _cached()
            known (patchdata.PatchInfo or suggest_tests.Extra or None): What
                the tests know, or None if every result is in 'cached'
            tests (dict): The tests, e.g. suggest_tests.PATCH_TESTS

        Returns:
            tuple or None: As for suggest_tests.score_results()
        """
        new = {}
        results = []
        for rule in rules:
            key = self._key(rule)
            if key not in cached:
                cached[key] = new[key] = tests[rule.test](rule.args, known)
            results.append(cached[key])
        cache = pwcache.get()
        if new and cache:
            cache.put_results(self.pwork.url, str(pws['id']),
                              {key: (res.matched, str(res.seen))
                               for key, res in new.items()})
        return suggest_tests.score_results(rules, results)

    async def _read_patches(self, client, pws):
        """Read the patches of a series for the tests, if allowed

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore

        Returns:
            patchdata.PatchInfo or None: What is known, or None if the
                patches cannot be read, or max_reads is reached
        """
        if self.reads >= self.rules.max_reads:
            self._capped = True
            self.complete = False
            return None
        try:
            return await self.patch_info(client, pws)
        except PW_ERRORS as exc:
            tout.warning(f"Cannot read the patches of "
                         f"'{pws.get('name')}': {exc}")
            return None

    async def _score(self, client, cand, found):
        """Run the tests which need the patches, or more, on a candidate

        Args:
            client (aiohttp.ClientSession): Session to use
            cand (Candidate): Candidate, from candidates()
            found (list of Suggestion): Series found so far

        Returns:
            Suggestion or None: The series, if it reaches min_score
        """
        cached = self._cached(cand.pws)
        rules = self._rules(suggest_tests.PATCH_TESTS)
        info = None
        if any(self._key(rule) not in cached for rule in rules):
            info = await self._read_patches(client, cand.pws)
            if not info:
                return None
        result = self._apply(cand.pws, rules, cached, info,
                             suggest_tests.PATCH_TESTS)
        if not result:
            return None
        score = cand.score + result[0]
        reasons = cand.reasons + result[1]
        if self._rules(suggest_tests.EXTRA_TESTS):
            result = await self._score_extra(client, cand.pws, info, score,
                                             found, cached)
            if not result:
                return None
            score += result[0]
            reasons = reasons + result[1]
        return (Suggestion(cand.pws, score, reasons)
                if score >= self.rules.min_score else None)

    # pylint: disable-next=R0913,R0917
    async def _score_extra(self, client, pws, info, score, found, cached):
        """Run the tests which need extra requests or git on a series

        They are not run if the series could not be suggested anyway

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore
            info (patchdata.PatchInfo or None): Its patches, if read
            score (int): Its score from the other tests
            found (list of Suggestion): Series found so far
            cached (dict): What the tests found before, from _cached()

        Returns:
            tuple or None: As for suggest_tests.score_series(), or None
                if the series is left out
        """
        rules = self._rules(suggest_tests.EXTRA_TESTS)
        best = score + _gain(rules)
        if best < self.rules.min_score:
            return None
        if not self._could_place(found, best):
            self.complete = False
            return None
        todo = {rule.test for rule in rules if self._key(rule) not in cached}
        extra = None
        if todo:
            # Blame needs the lines which the patches remove
            if 'blame' in todo and not info:
                info = await self._read_patches(client, pws)
                if not info:
                    return None
            try:
                extra = await self.extra(client, pws, info, todo)
            except PW_ERRORS as exc:
                tout.warning(f"Cannot check '{pws.get('name')}': {exc}")
                return None
        return self._apply(pws, rules, cached, extra,
                           suggest_tests.EXTRA_TESTS)

    async def _finish(self, cands):
        """Run the tests which need the patches, or more, for the best

        Args:
            cands (list of Candidate): Candidates, from candidates()

        Returns:
            list of Suggestion: Series which reach min_score
        """
        found = []
        async with aiohttp.ClientSession() as client:
            for cand in cands:
                if not self._could_place(found, cand.best):
                    self.complete = False
                    break
                sug = await self._score(client, cand, found)
                if sug:
                    found.append(sug)
        if self._capped:
            tout.warning(
                f'Read the patches of {self.reads} series, the most for one '
                "run (the 'max_reads' setting); the others are left out")
        return found

    def find(self, series):
        """Find the series to suggest

        Args:
            series (list of dict): Recent series, as from patchwork or lore

        Returns:
            list of Suggestion: Series which reach min_score, the best first,
                then the newest
        """
        cands = self.candidates(series)
        if (self._rules(suggest_tests.PATCH_TESTS) or
                self._rules(suggest_tests.EXTRA_TESTS)):
            found = common.run(self._finish(cands))
        else:
            found = [Suggestion(cand.pws, cand.score, cand.reasons)
                     for cand in cands if cand.score >= self.rules.min_score]
        found.sort(key=lambda sug: (sug.score, sug.pws.get('date') or ''),
                   reverse=True)
        return found


def series_title(pws):
    """Show the name of a series, with its version and number of patches

    Args:
        pws (dict): Series, as from patchwork or lore

    Returns:
        str: e.g. 'i2c: Add a driver (v2, 3 patches)'
    """
    count = pws.get('total') or len(pws.get('patches') or [])
    name = lookup.clean_series_name(pws.get('name') or '')
    return (f"{name} (v{pws.get('version') or 1}, {count} "
            f"patch{'es' if count != 1 else ''})")


# pylint: disable-next=R0913
def show_suggestions(found, total, limit, urls, reads=0, complete=True):
    """Show the series to suggest

    Args:
        found (list of Suggestion): Series to suggest, the best first
        total (int): Number of recent series looked at
        limit (int): Most series to show
        urls (dict): key (str): Series link
            value (str): URL of the series
        reads (int): Number of series whose patches were read
        complete (bool): True if every series which could be suggested was
            scored

    Returns:
        list of str: Lines to show
    """
    lines = []
    for sug in found[:limit]:
        lines.append(f'{sug.score:3}  {series_title(sug.pws)}')
        lines.append(f"       {urls[str(sug.pws['id'])]}")
        if sug.reasons:
            lines.append(f"       {', '.join(sug.reasons)}")
    shown = min(len(found), limit)
    summary = f'{shown} of {total} recent series suggested'
    if reads:
        summary += f', reading the patches of {reads}'
    if not complete:
        summary += '; --limit can show more'
    elif len(found) > limit:
        summary += f'; {len(found) - limit} more, which --limit can show'
    lines.append(summary)
    return lines


# pylint: disable-next=R0913,R0917
def suggestions_data(found, total, limit, sug, warnings, nrules):
    """Get the series to suggest, for 'patman serve' to show

    Args:
        found (list of Suggestion): Series to suggest, the best first
        total (int): Number of recent series looked at
        limit (int): Most series to give
        sug (Suggester): Suggester which found them
        warnings (list of str): Problems with the rules
        nrules (int): Number of rules

    Returns:
        dict: The number of 'rules', the 'total' looked at, the 'limit',
            the number of series whose patches were read ('reads'), whether
            every series was scored ('complete'), how many 'more' there are
            beyond the limit, the 'warnings' and the 'suggestions', each with
            its 'score', 'reasons' and 'series' as from patchwork or lore
    """
    return {'rules': nrules, 'total': total, 'limit': limit,
            'reads': sug.reads, 'complete': sug.complete,
            'more': max(0, len(found) - limit), 'warnings': warnings,
            'suggestions': [{'score': item.score, 'reasons': item.reasons,
                             'series': item.pws} for item in found[:limit]]}


def upstream_ref(args, cser, rules):
    """Get the branch to use for the history of the tree, if the rules need it

    The 'blame' test uses it for a series with no base-commit in the repo,
    and the 'newcomer' test for the authors of the commits in the tree

    Args:
        args (Namespace): Command-line arguments, for base_branch and
            upstream, if given
        cser (Cseries): Open cseries instance, for the default upstream
        rules (Rules): Rules to use

    Returns:
        str or None: Branch, e.g. 'us/next', or None if no rule needs it
    """
    if not any(rule.test in ('blame', 'newcomer') for rule in rules.rules):
        return None
    ref = apply.get_upstream_branch(SimpleNamespace(
        base_branch=getattr(args, 'base_branch', None),
        upstream=getattr(args, 'upstream', None)), cser)
    return ref if history.ref_exists('.', ref) else 'HEAD'


def _read_recent(pwork, since):
    """Read the series sent recently

    Args:
        pwork (Patchwork or LoreList): Where the series come from
        since (str): Date to start from, as YYYY-MM-DD

    Returns:
        list of dict: Series, newest first, as from patchwork or lore
    """
    async def _read():
        async with aiohttp.ClientSession() as client:
            return await scan.recent_series(
                client, pwork, since, partial=True, max_age=SUGGEST_MAX_AGE,
                consistent=True)

    return common.run(_read())


def _series_urls(pwork, found):
    """Get the link to each of some series, on patchwork or lore

    Args:
        pwork (Patchwork or LoreList): Where the series come from
        found (list of Suggestion): Series to link to

    Returns:
        dict: key (str): Series link
            value (str): URL of the series
    """
    return {str(sug.pws['id']): common.run(pwork.get_series_url(
        str(sug.pws['id']))) for sug in found}


def do_suggest(args, pwork, cser, own_emails, own_db):
    """Suggest series to review, by the rules in the settings

    Args:
        args (Namespace): Command-line arguments (for limit and json)
        pwork (Patchwork or LoreList): Where the series come from
        cser (Cseries): Open cseries instance with the reviews
        own_emails (set of str): Your email addresses, in lower case
        own_db (Database): Database holding your own series

    Returns:
        int: 0 on success, 1 if there are no rules (but 0 with --json,
            which says so)

    Raises:
        ValueError: A rule or setting is not valid, or the series cannot be
            read
    """
    rules = suggest_rules.get_rules()
    as_json = getattr(args, 'json', False)
    if not rules.rules:
        if as_json:
            print(json.dumps({'rules': 0}))
            return 0
        tout.error(suggest_rules.show_rules(rules, settings.review_project())[0])
        return 1
    warnings = suggest_rules.check_rules(rules,
                                         isinstance(pwork, lorelist.LoreList))
    if not as_json:
        for warning in warnings:
            tout.warning(warning)
    now = datetime.now(timezone.utc)
    series = _read_recent(pwork, (now - timedelta(days=rules.days)).strftime(
        '%Y-%m-%d'))
    limit = getattr(args, 'limit', None) or rules.limit
    sug = Suggester(rules, pwork, cser, own_emails, own_db, now, limit,
                    upstream_ref(args, cser, rules))
    found = sug.find(series)
    if as_json:
        print(json.dumps(suggestions_data(found, len(series), limit, sug,
                                          warnings, len(rules.rules))))
        return 0
    for line in show_suggestions(found, len(series), limit,
                                 _series_urls(pwork, found[:limit]),
                                 sug.reads, sug.complete):
        print(line)
    return 0
