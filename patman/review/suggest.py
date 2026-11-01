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
from types import SimpleNamespace

import aiohttp

from u_boot_pylib import gitutil
from u_boot_pylib import tout

from patman import lorelist
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
    """
    version = int(pws.get('version') or 1)
    existing = cser.db.series_find_by_link(str(pws['id']))
    if existing and cser.db.review_get_for_version(existing[3]):
        return True, False
    earlier = scan.find_reviewed(cser, pws, by_key)
    return False, bool(earlier) and (
        cser.db.series_get_max_version(earlier) or 0) < version


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
    series left could get into the top 'limit', or max_reads series have
    been read

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
        done, before = _reviewed(pws, self.cser, self._by_key())
        states = self.pwork.get_states([pat['id'] for pat in
                                        pws.get('patches') or []])
        return done, suggest_tests.series_info(pws, self.now, before,
                                 list(states.values()))

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

    async def extra(self, client, pws, info):
        """Find what the tests which need extra requests or git need

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore
            info (patchdata.PatchInfo or None): Its patches, if read

        Returns:
            suggest_tests.Extra: What is known

        Raises:
            ValueError: Something which is needed cannot be read
        """
        blame = {}
        if info and info.removed and self._uses('blame'):
            top = self.git_dir()
            ref = (info.base if info.base and history.ref_exists(
                top, info.base) else self.upstream)
            if ref:
                blame = history.blame_mine(top, ref, info.removed,
                                           self.emails)
        newcomer, how = False, ''
        if self._uses('newcomer'):
            newcomer, how = await self._newcomer(client, pws)
        return suggest_tests.Extra(blame, newcomer, how)

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

    async def _score(self, client, cand, found):
        """Run the tests which need the patches, or more, on a candidate

        Args:
            client (aiohttp.ClientSession): Session to use
            cand (Candidate): Candidate, from candidates()
            found (list of Suggestion): Series found so far

        Returns:
            Suggestion or None: The series, if it reaches min_score
        """
        info = None
        if self.needs_patches():
            try:
                info = await self.patch_info(client, cand.pws)
            except PW_ERRORS as exc:
                tout.warning(f"Cannot read the patches of "
                             f"'{cand.pws.get('name')}': {exc}")
                return None
        result = suggest_tests.score_series(
            self._rules(suggest_tests.PATCH_TESTS), info,
            suggest_tests.PATCH_TESTS)
        if not result:
            return None
        score = cand.score + result[0]
        reasons = cand.reasons + result[1]
        if self._rules(suggest_tests.EXTRA_TESTS):
            result = await self._score_extra(client, cand.pws, info, score,
                                             found)
            if not result:
                return None
            score += result[0]
            reasons = reasons + result[1]
        return (Suggestion(cand.pws, score, reasons)
                if score >= self.rules.min_score else None)

    # pylint: disable-next=R0913
    async def _score_extra(self, client, pws, info, score, found):
        """Run the tests which need extra requests or git on a series

        They are not run if the series could not be suggested anyway

        Args:
            client (aiohttp.ClientSession): Session to use
            pws (dict): Series, as from patchwork or lore
            info (patchdata.PatchInfo or None): Its patches, if read
            score (int): Its score from the other tests
            found (list of Suggestion): Series found so far

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
        try:
            extra = await self.extra(client, pws, info)
        except PW_ERRORS as exc:
            tout.warning(f"Cannot check '{pws.get('name')}': {exc}")
            return None
        return suggest_tests.score_series(rules, extra,
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
                if self.needs_patches() and (
                        self.reads >= self.rules.max_reads):
                    tout.warning(
                        f'Read the patches of {self.reads} series, the most '
                        "for one run (the 'max_reads' setting); the others "
                        'are left out')
                    self.complete = False
                    break
                sug = await self._score(client, cand, found)
                if sug:
                    found.append(sug)
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
        args (Namespace): Command-line arguments (for limit)
        pwork (Patchwork or LoreList): Where the series come from
        cser (Cseries): Open cseries instance with the reviews
        own_emails (set of str): Your email addresses, in lower case
        own_db (Database): Database holding your own series

    Returns:
        int: 0 on success, 1 if there are no rules

    Raises:
        ValueError: A rule or setting is not valid, or the series cannot be
            read
    """
    rules = suggest_rules.get_rules()
    if not rules.rules:
        tout.error(suggest_rules.show_rules(rules, settings.review_project())[0])
        return 1
    for warning in suggest_rules.check_rules(rules, isinstance(pwork, lorelist.LoreList)):
        tout.warning(warning)
    for rule in rules.rules:
        if rule.test not in suggest_tests.ALL_TESTS:
            tout.warning(f"Suggest rule '{rule.name}' uses '{rule.test}', "
                         'which --suggest does not run yet, so it is left out')
    now = datetime.now(timezone.utc)
    series = _read_recent(pwork, (now - timedelta(days=rules.days)).strftime(
        '%Y-%m-%d'))
    limit = getattr(args, 'limit', None) or rules.limit
    sug = Suggester(rules, pwork, cser, own_emails, own_db, now, limit,
                    upstream_ref(args, cser, rules))
    found = sug.find(series)
    for line in show_suggestions(found, len(series), limit,
                                 _series_urls(pwork, found[:limit]),
                                 sug.reads, sug.complete):
        print(line)
    return 0
