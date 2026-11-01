# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Suggest series to review, by your own rules

Each person chooses which series to review in their own way, e.g. by the
files a series touches, who sent it or whether anyone has reviewed it yet.
The rules for this are in the [suggest] section of the settings, one to a
line, each with a name of your choosing:

    i2c:      path drivers/i2c/**  +3
    newcomer: newcomer  +2
    bots:     from *bot*@*  skip
    limit:    8

A rule's value is an optional 'not', a test, the test's arguments and then
a score: '+N' or '-N' to add to the series' score, 'skip' to leave the
series out or 'need' to leave it out unless the rule matches. A value of
'off' turns off a rule of the same name from a more general section, so a
project's section, e.g. [linux_suggest], can drop one of your general rules.

The tests differ in how much they cost to run, from those which need only
the list of series, to those which need each patch or extra requests, so the
rules are kept in order of cost, to run the cheap ones first.
"""

from collections import namedtuple
from datetime import datetime, timedelta, timezone
import fnmatch
import re
import shlex

import aiohttp

from u_boot_pylib import tout

from patman import lorelist
from patman import settings
from patman.review import common
from patman.review import lookup
from patman.review import scan

# How much a test costs to run: the list of series is enough for FREE, PATCH
# needs each patch of the series, and EXTRA needs another request, or work
# in the git repo, for each series
FREE, PATCH, EXTRA = range(3)

# What each cost is called, when showing the rules
COST_NAMES = {
    FREE: 'From the list of series',
    PATCH: 'From each patch',
    EXTRA: 'With extra requests or git',
}

# The kinds of arguments which a test takes:
#   none: no arguments
#   globs: one or more file or address patterns, e.g. 'drivers/i2c/**'
#   words: one or more words, e.g. 'boot:'
#   regex: one or more regular expressions
#   range: one number or range, e.g. '25-' (see common.split_range())
#   roles: 'M' and 'R', or neither for both
#   me-none: 'me' or 'none'
#   pass-fail: 'pass' or 'fail'
Test = namedtuple('Test', 'args,cost,patchwork,desc')

TESTS = {
    'prefix': Test('words', FREE, False,
                   'a patch subject starts with one of these'),
    'from': Test('globs', FREE, False,
                 "the submitter's address or name matches"),
    'patches': Test('range', FREE, False, 'the number of patches'),
    'version': Test('range', FREE, False, 'the version of the series'),
    'age': Test('range', FREE, False, 'days since the series was sent'),
    'rfc': Test('none', FREE, False, 'the series is an RFC'),
    'reviewed-before': Test('none', FREE, False,
                            'you reviewed an earlier version'),
    'state': Test('words', FREE, True,
                  'a patch is in one of these patchwork states'),
    'path': Test('globs', PATCH, False, 'a patch touches a matching file'),
    'only-path': Test('globs', PATCH, False, 'every file touched matches'),
    'lines': Test('range', PATCH, False,
                  'the number of lines added and removed'),
    'maintainer': Test('roles', PATCH, False,
                       'you are in MAINTAINERS for a file touched'),
    'word': Test('regex', PATCH, False,
                 'the diff adds or removes a matching line'),
    'has': Test('words', PATCH, False,
                'a patch has one of these tags, e.g. reviewed-by'),
    'to-me': Test('none', PATCH, False, 'you are in To: or Cc: of a patch'),
    'no-replies': Test('range', PATCH, False,
                       'days since it was sent, with no replies'),
    'fixes': Test('none', PATCH, False,
                  'a Fixes: tag names one of your commits'),
    'delegate': Test('me-none', PATCH, True,
                     'the patchwork delegate is you, or nobody'),
    'checks': Test('pass-fail', EXTRA, True, "patchwork's CI result"),
    'blame': Test('none', EXTRA, False, 'it changes lines you wrote'),
    'newcomer': Test('none', EXTRA, False,
                     'the submitter has no earlier series'),
}

# The settings in [suggest] which are not rules, with their defaults
FIXED = {'limit': 10, 'min_score': 1, 'days': 30}

# A score which adds to the series' score, e.g. '+3' or '-2'
RE_SCORE = re.compile(r'^[+-]\d+$')

# A rule: its 'name', the 'test' (a key of TESTS), the test's 'args' (see
# _parse_args()), whether the result is negated ('negate'), and the score,
# which is an int, or 'skip' or 'need'
Rule = namedtuple('Rule', 'name,test,args,negate,score')

# The rules, in order of cost, then name, with the fixed settings
Rules = namedtuple('Rules', 'rules,limit,min_score,days')


def _parse_list(test, kind, words):
    """Parse the arguments of a test which takes a list

    Args:
        test (str): Name of the test, a key of TESTS
        kind (str): Kind of arguments: 'globs', 'words' or 'regex'
        words (list of str): Its arguments

    Returns:
        tuple: The arguments, as given, with words in lower case for 'has'
            and 'state', or the compiled patterns for regex

    Raises:
        ValueError: There are no arguments, or a pattern is not valid
    """
    if not words:
        raise ValueError(f"'{test}' needs at least one argument")
    if kind == 'regex':
        try:
            return tuple(re.compile(word) for word in words)
        except re.error as exc:
            raise ValueError(f"'{test}' has a bad pattern: {exc}") from exc
    if test in ('has', 'state'):
        return tuple(word.lower() for word in words)
    return tuple(words)


def _parse_range(test, words):
    """Parse the argument of a test which takes a range

    Args:
        test (str): Name of the test, a key of TESTS
        words (list of str): Its arguments

    Returns:
        tuple: The two ends of the range (see common.split_range())

    Raises:
        ValueError: There is not exactly one range, or it is back to front
    """
    ends = len(words) == 1 and common.split_range(words[0])
    if not ends:
        raise ValueError(f"'{test}' needs one number or range, e.g. '5', "
                         "'3-10', '-3' or '25-'")
    if None not in ends and ends[0] > ends[1]:
        raise ValueError(f"'{test}' has a range which is back to front")
    return ends


def _parse_args(test, words):
    """Parse the arguments of a test

    Args:
        test (str): Name of the test, a key of TESTS
        words (list of str): Its arguments

    Returns:
        tuple: The arguments (see _parse_list() and _parse_range()), or the
            roles, or the one word for me-none and pass-fail

    Raises:
        ValueError: The arguments are not valid for the test
    """
    kind = TESTS[test].args
    if kind == 'none':
        if words:
            raise ValueError(f"'{test}' takes no arguments")
        return ()
    if kind in ('globs', 'words', 'regex'):
        return _parse_list(test, kind, words)
    if kind == 'range':
        return _parse_range(test, words)
    if kind == 'roles':
        roles = tuple(word.upper() for word in words)
        if any(role not in ('M', 'R') for role in roles):
            raise ValueError(f"'{test}' takes only M and R")
        return roles or ('M', 'R')
    choices = kind.split('-')
    if len(words) != 1 or words[0].lower() not in choices:
        raise ValueError(f"'{test}' needs {' or '.join(choices)}")
    return (words[0].lower(),)


def parse_rule(name, value):
    """Parse one rule from the settings

    Args:
        name (str): Name of the rule
        value (str): The rule, e.g. 'not path doc/** +2'

    Returns:
        Rule or None: The rule, or None if it is 'off'

    Raises:
        ValueError: The rule is not valid
    """
    try:
        words = shlex.split(value)
    except ValueError as exc:
        raise ValueError(f"Suggest rule '{name}': {exc}") from exc
    if words == ['off']:
        return None
    negate = bool(words) and words[0] == 'not'
    if negate:
        words = words[1:]
    if len(words) < 2:
        raise ValueError(f"Suggest rule '{name}' needs a test and a score, "
                         "e.g. 'path drivers/i2c/** +3'")
    test, *args, score = words
    if test not in TESTS:
        raise ValueError(f"Suggest rule '{name}' has an unknown test '{test}'"
                         f"; use one of: {', '.join(sorted(TESTS))}")
    if RE_SCORE.match(score):
        score = int(score)
    elif score not in ('skip', 'need'):
        raise ValueError(f"Suggest rule '{name}' ends with '{score}', not a "
                         "score: use e.g. +3 or -2, or skip or need")
    try:
        args = _parse_args(test, args)
    except ValueError as exc:
        raise ValueError(f"Suggest rule '{name}': {exc}") from exc
    return Rule(name, test, args, negate, score)


def _parse_fixed(name, value):
    """Parse one of the fixed settings, e.g. 'limit'

    Args:
        name (str): Name of the setting, a key of FIXED
        value (str): Its value

    Returns:
        int: The value

    Raises:
        ValueError: The value is not valid
    """
    try:
        num = int(value)
    except ValueError:
        num = None
    if num is None or (name != 'min_score' and num < 1):
        what = 'a number' if name == 'min_score' else 'a number above 0'
        raise ValueError(f"The suggest setting '{name}' needs {what}, not "
                         f"'{value}'")
    return num


def get_rules(values=None):
    """Get the rules for suggesting series, from the [suggest] settings

    Args:
        values (dict or None): Settings, or None to use those read from the
            config (settings.suggest)

    Returns:
        Rules: The rules, in order of cost then name, with the fixed settings

    Raises:
        ValueError: A rule or setting is not valid
    """
    values = settings.suggest if values is None else values
    fixed = dict(FIXED)
    rules = []
    for name, value in values.items():
        if name in FIXED:
            fixed[name] = _parse_fixed(name, value)
            continue
        rule = parse_rule(name, value)
        if rule:
            rules.append(rule)
    rules.sort(key=lambda rule: (TESTS[rule.test].cost, rule.name))
    return Rules(rules, fixed['limit'], fixed['min_score'], fixed['days'])


def check_rules(rules, lore):
    """Find the rules which can never match

    Args:
        rules (Rules): Rules to check
        lore (bool): True if the series come from lore, which has no
            patchwork states, delegates or checks

    Returns:
        list of str: Warnings, one for each problem found
    """
    warnings = []
    if lore:
        for rule in rules.rules:
            if TESTS[rule.test].patchwork:
                warnings.append(f"Suggest rule '{rule.name}' uses "
                                f"'{rule.test}', which lore does not have, "
                                'so it never matches')
    if rules.rules and rules.min_score > 0 and not any(
            isinstance(rule.score, int) and rule.score > 0
            for rule in rules.rules):
        warnings.append('No suggest rule adds to the score, so no series '
                        f'reaches min_score {rules.min_score}')
    return warnings


def _show_args(rule):
    """Show the arguments of a rule as they would be written

    Args:
        rule (Rule): Rule to show

    Returns:
        str: The arguments, e.g. 'drivers/i2c/**' or '25-'
    """
    kind = TESTS[rule.test].args
    if kind == 'range':
        start, end = rule.args
        if start == end:
            return str(start)
        return f"{'' if start is None else start}-{'' if end is None else end}"
    if kind == 'regex':
        return shlex.join(pat.pattern for pat in rule.args)
    return shlex.join(rule.args)


def show_rules(rules, project):
    """Show the rules, as understood from the settings

    Args:
        rules (Rules): Rules to show
        project (str): Name of the project the rules are for, e.g. 'U-Boot'

    Returns:
        list of str: Lines to show
    """
    if not rules.rules:
        return [f'No rules for suggesting {project} series: add them to the '
                '[suggest] section of the settings']
    lines = [f'Rules for suggesting {project} series:']
    width = max(len(rule.name) for rule in rules.rules)
    cost = None
    for rule in rules.rules:
        if TESTS[rule.test].cost != cost:
            cost = TESTS[rule.test].cost
            lines.append(f'  {COST_NAMES[cost]}:')
        test = f"{'not ' if rule.negate else ''}{rule.test}"
        args = _show_args(rule)
        score = (f'{rule.score:+d}' if isinstance(rule.score, int)
                 else rule.score)
        lines.append(f"    {rule.name:{width}}  {test}{' ' if args else ''}"
                     f'{args}  {score}')
    lines.append(f'Suggest at most {rules.limit}, with a score of at least '
                 f'{rules.min_score}, sent in the last {rules.days} days')
    return lines


def do_rules(pwork):
    """Check the rules for suggesting series and show them

    Args:
        pwork (Patchwork or LoreList): Where the series come from

    Returns:
        int: 0 if the rules are valid and can match, else 1

    Raises:
        ValueError: A rule or setting is not valid
    """
    rules = get_rules()
    for line in show_rules(rules, settings.review_project()):
        print(line)
    warnings = check_rules(rules, isinstance(pwork, lorelist.LoreList))
    for warning in warnings:
        tout.warning(warning)
    return 1 if warnings or not rules.rules else 0


# The [...] tags at the start of a subject, e.g. '[PATCH v2 1/3] '
RE_TAGS = re.compile(r'^(\[[^]]*\]\s*)+')

# What the tests which need only the list of series know about a series:
#   subjects (list of str): Subjects of its patches, without their tags
#   email (str): Submitter's address, in lower case
#   who (str): Submitter's name, in lower case
#   patches (int): Number of patches
#   version (int): Version of the series
#   age (int): Whole days since it was sent
#   rfc (bool): True if it is an RFC
#   reviewed_before (bool): True if you reviewed an earlier version
#   states (list of str): Patchwork states of its patches, where known
Info = namedtuple('Info', 'subjects,email,who,patches,version,age,rfc,'
                          'reviewed_before,states')

# A series to suggest: the series ('pws', as from patchwork or lore), its
# score and the reasons for it, e.g. ['i2c +3']
Suggestion = namedtuple('Suggestion', 'pws,score,reasons')


def _in_range(value, ends):
    """Check whether a number is in a range from a rule

    Args:
        value (int): Number to check
        ends (tuple): Start and end, either of which may be None for none

    Returns:
        bool: True if it is in the range
    """
    start, end = ends
    return (start is None or value >= start) and (end is None or value <= end)


def _matches_from(pats, info):
    """Check whether the submitter matches any of some patterns

    Args:
        pats (tuple of str): Patterns, for the address or the name
        info (Info): The series

    Returns:
        bool: True if the address or name matches one of them
    """
    return any(fnmatch.fnmatchcase(text, pat.lower()) for pat in pats
               for text in (info.email, info.who) if text)


# The tests which need only the list of series, each taking the rule's
# arguments and the Info of a series
FREE_TESTS = {
    'prefix': lambda args, info: any(
        subj.lower().startswith(word.lower())
        for subj in info.subjects for word in args),
    'from': _matches_from,
    'patches': lambda args, info: _in_range(info.patches, args),
    'version': lambda args, info: _in_range(info.version, args),
    'age': lambda args, info: _in_range(info.age, args),
    'rfc': lambda args, info: info.rfc,
    'reviewed-before': lambda args, info: info.reviewed_before,
    'state': lambda args, info: any(state in args for state in info.states),
}


def _age_days(date, now):
    """Get the whole days since a series was sent

    Args:
        date (str): When it was sent, in ISO format, in UTC if it gives no
            time zone, as lore and some patchwork servers do
        now (datetime): The time now, with its time zone

    Returns:
        int: Days since then, or 0 if the date is not known
    """
    try:
        when = datetime.fromisoformat((date or '').replace('Z', '+00:00'))
    except ValueError:
        return 0
    if not when.tzinfo:
        when = when.replace(tzinfo=timezone.utc)
    return max((now - when).days, 0)


def series_info(pws, now, reviewed_before, states):
    """Get what the free tests need to know about a series

    Args:
        pws (dict): Series, as from patchwork or lore
        now (datetime): The time now, with its time zone
        reviewed_before (bool): True if you reviewed an earlier version
        states (list of str): Patchwork states of its patches, where known

    Returns:
        Info: What is known
    """
    patches = pws.get('patches') or []
    names = [pat.get('name') or '' for pat in patches]
    cover = (pws.get('cover_letter') or {}).get('name') or ''
    sub = pws.get('submitter') or {}
    tags = ' '.join(RE_TAGS.match(name).group(0)
                    for name in (cover, pws.get('name') or '', *names)
                    if RE_TAGS.match(name))
    return Info(
        [RE_TAGS.sub('', name).strip() for name in names] or
        [RE_TAGS.sub('', pws.get('name') or '').strip()],
        (sub.get('email') or '').lower(), (sub.get('name') or '').lower(),
        pws.get('total') or len(patches), int(pws.get('version') or 1),
        _age_days(pws.get('date'), now),
        bool(re.search(r'\bRFC\b', tags, re.I)), reviewed_before, states)


def score_series(rules, info):
    """Work out the score of a series, by the rules

    Args:
        rules (list of Rule): Rules to use, each with a test in FREE_TESTS
        info (Info): The series

    Returns:
        tuple or None: None if a rule leaves the series out, else:
            int: Its score
            list of str: Reasons for the score, e.g. ['i2c +3']
    """
    score = 0
    reasons = []
    for rule in rules:
        matched = FREE_TESTS[rule.test](rule.args, info) != rule.negate
        if rule.score == 'skip':
            if matched:
                return None
        elif rule.score == 'need':
            if not matched:
                return None
        elif matched:
            score += rule.score
            reasons.append(f'{rule.name} {rule.score:+d}')
    return score, reasons


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


# pylint: disable-next=R0913
def find_suggestions(rules, series, pwork, cser, own_emails, own_db, now):
    """Find the series to suggest, of those sent recently

    Your own series are left out, as are those you have already reviewed at
    this version, and older versions of a series

    Args:
        rules (Rules): Rules to use, keeping only those in FREE_TESTS
        series (list of dict): Recent series, as from patchwork or lore
        pwork (Patchwork or LoreList): Where the series come from
        cser (Cseries): Open cseries instance with the reviews
        own_emails (set of str): Your email addresses, in lower case
        own_db (Database): Database holding your own series
        now (datetime): The time now, with its time zone

    Returns:
        list of Suggestion: Every series which reaches min_score, the best
            first, then the newest
    """
    usable = [rule for rule in rules.rules if rule.test in FREE_TESTS]
    by_key = lookup.b4_series(cser.db)
    found = []
    for pws in _newest(series):
        if lookup.is_own_series(pws, own_emails, own_db):
            continue
        done, before = _reviewed(pws, cser, by_key)
        if done:
            continue
        states = pwork.get_states([pat['id']
                                   for pat in pws.get('patches') or []])
        result = score_series(usable, series_info(pws, now, before,
                                                  list(states.values())))
        if result and result[0] >= rules.min_score:
            found.append(Suggestion(pws, *result))
    found.sort(key=lambda sug: (sug.score, sug.pws.get('date') or ''),
               reverse=True)
    return found


def show_suggestions(found, total, limit, urls):
    """Show the series to suggest

    Args:
        found (list of Suggestion): Series to suggest, the best first
        total (int): Number of recent series looked at
        limit (int): Most series to show
        urls (dict): key (str): Series link
            value (str): URL of the series

    Returns:
        list of str: Lines to show
    """
    lines = []
    for sug in found[:limit]:
        pws = sug.pws
        count = pws.get('total') or len(pws.get('patches') or [])
        name = lookup.clean_series_name(pws.get('name') or '')
        lines.append(f"{sug.score:3}  {name} (v{pws.get('version') or 1}, "
                     f"{count} patch{'es' if count != 1 else ''})")
        lines.append(f"       {urls[str(pws['id'])]}")
        if sug.reasons:
            lines.append(f"       {', '.join(sug.reasons)}")
    shown = min(len(found), limit)
    summary = f'{shown} of {total} recent series suggested'
    if len(found) > limit:
        summary += f'; {len(found) - limit} more, which --limit can show'
    lines.append(summary)
    return lines


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
            return await scan.recent_series(client, pwork, since,
                                            partial=True)

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
    rules = get_rules()
    if not rules.rules:
        tout.error(show_rules(rules, settings.review_project())[0])
        return 1
    for warning in check_rules(rules, isinstance(pwork, lorelist.LoreList)):
        tout.warning(warning)
    for rule in rules.rules:
        if rule.test not in FREE_TESTS:
            tout.warning(f"Suggest rule '{rule.name}' uses '{rule.test}', "
                         'which --suggest does not run yet, so it is left out')
    now = datetime.now(timezone.utc)
    series = _read_recent(pwork, (now - timedelta(days=rules.days)).strftime(
        '%Y-%m-%d'))
    found = find_suggestions(rules, series, pwork, cser, own_emails, own_db,
                             now)
    limit = getattr(args, 'limit', None) or rules.limit
    for line in show_suggestions(found, len(series), limit,
                                 _series_urls(pwork, found[:limit])):
        print(line)
    return 0
