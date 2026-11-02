# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""The tests which the rules for suggesting series use

Each test takes the arguments of a rule and what is known about a series,
and gives a Result: whether it matches and what it saw, which --explain
shows. The tests are grouped by what they need: FREE_TESTS need only the
list of series, PATCH_TESTS the patches of the series, and EXTRA_TESTS
extra requests or git (see suggest.Suggester, which finds these out).
"""

from collections import namedtuple
from datetime import datetime, timezone
import fnmatch
import re

from patman.review import patchdata

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


# What a test finds: whether it 'matched', and what it 'seen', to show with
# --explain, e.g. the files which matched, or else the files touched
Result = namedtuple('Result', 'matched,seen')


# Most items to show of what a test saw, e.g. the files a series touches
SHOW_ITEMS = 3


def _seen(items):
    """Show some of the items which a test saw

    Args:
        items (iterable of str): Items, e.g. files

    Returns:
        str: The first SHOW_ITEMS of them, saying how many more there are, or
            'none' if there are none
    """
    items = list(items)
    out = ', '.join(items[:SHOW_ITEMS])
    if len(items) > SHOW_ITEMS:
        out += f' and {len(items) - SHOW_ITEMS} more'
    return out or 'none'


def _found(found, others):
    """Get the result of a test which looks for some items

    Args:
        found (list of str): Items which match
        others (list of str): Items to show if none match, e.g. all of them

    Returns:
        Result: Matched if any item matches
    """
    return Result(bool(found), _seen(found or others))


def in_range(value, ends):
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


# The prefix of a subject, e.g. 'boot: bootm: ' in 'boot: bootm: Fix it'
RE_PREFIX = re.compile(r'^(?:[\w./-]+: )+')


def _prefixes(subjects):
    """Get the prefixes of some subjects, to show

    Args:
        subjects (list of str): Subjects, without their tags

    Returns:
        list of str: Their prefixes, e.g. 'boot: bootm:', each once
    """
    out = []
    for subj in subjects:
        match = RE_PREFIX.match(subj)
        prefix = match.group(0).strip() if match else subj
        if prefix not in out:
            out.append(prefix)
    return out


def _days(count):
    """Say how many days old a series is

    Args:
        count (int): Whole days

    Returns:
        str: e.g. '1 day' or '3 days'

    >>> _days(1), _days(3)
    ('1 day', '3 days')
    """
    return f"{count} day{'s' if count != 1 else ''}"


def _yes_no(value):
    """Show whether something is so

    Args:
        value (bool): Whether it is so

    Returns:
        str: 'yes' or 'no'
    """
    return 'yes' if value else 'no'


# The tests which need only the list of series, each taking the rule's
# arguments and the Info of a series, and giving a Result
FREE_TESTS = {
    'prefix': lambda args, info: _found(
        _prefixes([subj for subj in info.subjects
                   if any(subj.lower().startswith(word.lower())
                          for word in args)]), _prefixes(info.subjects)),
    'from': lambda args, info: Result(_matches_from(args, info),
                                      info.email or info.who or 'none'),
    'patches': lambda args, info: Result(in_range(info.patches, args),
                                         str(info.patches)),
    'version': lambda args, info: Result(in_range(info.version, args),
                                         f'v{info.version}'),
    'age': lambda args, info: Result(in_range(info.age, args),
                                     _days(info.age)),
    'rfc': lambda args, info: Result(info.rfc, _yes_no(info.rfc)),
    'reviewed-before': lambda args, info: Result(
        info.reviewed_before, _yes_no(info.reviewed_before)),
    'state': lambda args, info: _found(
        sorted({state for state in info.states if state in args}),
        sorted(set(info.states))),
}


def age_days(date, now):
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
        age_days(pws.get('date'), now),
        bool(re.search(r'\bRFC\b', tags, re.I)), reviewed_before, states)


def score_results(rules, results):
    """Work out the score of a series, from what the tests of the rules found

    Args:
        rules (list of Rule): Rules used
        results (list of Result): What the test of each rule found

    Returns:
        tuple or None: None if a rule leaves the series out, else:
            int: Its score
            list of str: Reasons for the score, e.g. ['i2c +3']
    """
    score = 0
    reasons = []
    for rule, res in zip(rules, results):
        matched = res.matched != rule.negate
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


def score_series(rules, info, tests):
    """Work out the score of a series, by the rules

    Args:
        rules (list of Rule): Rules to use, each with a test in 'tests'
        info (Info or patchdata.PatchInfo): What the tests know about the
            series
        tests (dict): Tests to run, e.g. FREE_TESTS

    Returns:
        tuple or None: As for score_results()
    """
    return score_results(rules, [tests[rule.test](rule.args, info)
                                 for rule in rules])


def _paths(pats, info):
    """Find the files which some patterns match

    Args:
        pats (tuple of str): Patterns, as for patchdata.glob_match()
        info (patchdata.PatchInfo): The series

    Returns:
        list of str: Files touched which match one of them
    """
    return [name for name in info.files
            if any(patchdata.glob_match(pat, name) for pat in pats)]


def _only_paths(pats, info):
    """Check whether every file touched matches some patterns

    Args:
        pats (tuple of str): Patterns, as for patchdata.glob_match()
        info (patchdata.PatchInfo): The series

    Returns:
        Result: Matched if all do, showing those which do not
    """
    others = [name for name in info.files if name not in _paths(pats, info)]
    return Result(bool(info.files) and not others,
                  _seen(others or info.files))


# The tests which need the patches, each taking the rule's arguments and
# the patchdata.PatchInfo of a series, and giving a Result
PATCH_TESTS = {
    'path': lambda args, info: _found(_paths(args, info), info.files),
    'only-path': _only_paths,
    'lines': lambda args, info: Result(in_range(len(info.changed), args),
                                       f'{len(info.changed)} lines'),
    'maintainer': lambda args, info: Result(bool(set(args) & info.roles),
                                            _seen(sorted(info.roles))),
    'word': lambda args, info: _found(
        [line.strip() for line in info.changed
         if any(pat.search(line) for pat in args)],
        [f'{len(info.changed)} lines']),
    'has': lambda args, info: _found(sorted(set(args) & info.tags),
                                     sorted(info.tags)),
    'to-me': lambda args, info: Result(info.to_me, _yes_no(info.to_me)),
    'fixes': lambda args, info: _found(info.own_fixes, info.fixes),
}


# What the tests which need extra requests or git know about a series:
#   blame (dict): Lines you wrote among those it changes (see
#       history.blame_mine())
#   newcomer (bool): True if its submitter has sent no earlier series
#   how (str): How that was decided, e.g. 'has commits in the tree'
#   age (int): Whole days since it was sent
#   replier (str or None): Name of the first person other than the submitter
#       to reply to it, '' if nobody has, or None if not looked at, since
#       the series is not of an age which a rule wants
#   delegates (list of str or None): Patchwork delegate of each patch which
#       has one, or None if patchwork does not say, e.g. on lore
#   delegate_me (bool): True if a patch is delegated to you
#   checks (list of str or None): Patchwork's CI result for each patch, e.g.
#       'success' or 'pending', or None if patchwork does not say
Extra = namedtuple('Extra', 'blame,newcomer,how,age,replier,delegates,'
                   'delegate_me,checks', defaults=(0, None, None, False, None))


def _no_replies(args, extra):
    """Check whether nobody but the submitter has replied to a series

    Args:
        args (tuple): Range of days since the series was sent
        extra (Extra): What is known

    Returns:
        Result: What was found
    """
    if extra.replier:
        return Result(False, f'reply from {extra.replier}')
    if extra.replier is None:
        return Result(False, _days(extra.age))
    return Result(in_range(extra.age, args),
                  f'no replies, {_days(extra.age)}')


def _delegate(args, extra):
    """Check who the patchwork delegate of a series is

    Args:
        args (tuple): 'me' or 'none'
        extra (Extra): What is known

    Returns:
        Result: What was found
    """
    if extra.delegates is None:
        return Result(False, 'not known')
    matched = extra.delegate_me if args[0] == 'me' else not extra.delegates
    return Result(matched, _seen(sorted(set(extra.delegates))) or 'none')


def _checks(args, extra):
    """Check patchwork's CI result for a series

    It passes if every patch passes and fails if any patch fails

    Args:
        args (tuple): 'pass' or 'fail'
        extra (Extra): What is known

    Returns:
        Result: What was found
    """
    if not extra.checks:
        return Result(False, 'not known')
    if args[0] == 'pass':
        matched = all(state == 'success' for state in extra.checks)
    else:
        matched = 'fail' in extra.checks
    counts = {state: extra.checks.count(state)
              for state in sorted(set(extra.checks))}
    return Result(matched, ', '.join(f'{count} {state}'
                                     for state, count in counts.items()))


# The tests which need extra requests or git, each taking the rule's
# arguments and the Extra of a series, and giving a Result
EXTRA_TESTS = {
    'blame': lambda args, extra: Result(bool(extra.blame), _seen(
        f'{count} in {fname}' for fname, count in extra.blame.items())),
    'newcomer': lambda args, extra: Result(extra.newcomer, extra.how),
    'no-replies': _no_replies,
    'delegate': _delegate,
    'checks': _checks,
}


# Every test which can be run
ALL_TESTS = {**FREE_TESTS, **PATCH_TESTS, **EXTRA_TESTS}
