# patman: suggesting series to review

Collected 2026-11-01.

## Problem

The inbox lists every series sent in the last 30 days, grouped by who has
the next move. For a series you have not touched, it says nothing about
whether you would want to review it, so you have to read through the
titles and pick out the ones in your area, from people you work with, or
that nobody else is looking at.

How people choose differs a lot. A custodian wants anything touching their
subsystem. A mentor wants series from newcomers. A core developer wants
changes to code they wrote, and fixes to their own commits. Someone short of
time wants small series that nobody has reviewed yet. These choices are
rules, which can be written down once as settings and then applied to each
new series, with the reasons shown so that you can see why each series is
suggested.

## Goals

1. Each person writes their own rules in the settings, in the same file
   and style as the other patman settings
2. 'patman review --suggest' lists the series worth reviewing, best first,
   each with its score and the rules which gave it
3. The inbox in 'patman serve' shows the same suggestions as a group
4. Suggesting adds little load on patchwork, especially ozlabs: the cheap
   rules run first, and the costly ones only on the series still in the
   running

Not goals for now: learning rules from past reviews, or sharing rules
through a project's .patman-defaults (though nothing stops that later).

## 1. Settings

A new [suggest] section, read like [review] and [maint] (see
settings._read_maint()), warning about unknown fixed keys.

```ini
[suggest]
# <name>: [not] <test> [args...] <score>
i2c:       path drivers/i2c/** arch/arm/mach-bcm283x/**  +3
dm:        path drivers/core/** include/dm/**  +3
listed:    maintainer R M  +3
my-code:   blame  +2
my-fix:    fixes  +4
boot:      prefix boot: bootstd: pxe:  +2
friends:   from *@chromium.org alice@example.com  +2
newcomer:  newcomer  +2
cc-me:     to-me  +1
again:     reviewed-before  +3
reviewed:  has reviewed-by acked-by  -3
quiet:     no-replies 3-  +1
bots:      from *bot*@*  skip
huge:      patches 25-  skip
rfc:       rfc  -1
docs-only: only-path doc/**  -2

limit: 8
min_score: 2
days: 14

[linux_suggest]
i2c:  path drivers/i2c/busses/i2c-bcm2835.c  +3
dm:   off
```

### Rules

- The key is a name you choose. It is shown in the reasons, e.g.
  'i2c +3', so it should say what the rule is for
- The value is an optional 'not', a test, the test's arguments, then a
  score
- Arguments are split as by shlex, so a pattern with spaces is quoted,
  e.g. `word "struct udevice \*"`
- A key whose value is 'off' turns off a rule of that name from a more
  general section

### Scores

- '+N' or '-N' is added to the series' score when the rule matches
- 'skip' leaves the series out, whatever its score
- 'need' leaves the series out unless the rule matches, e.g. to see only
  series touching your subsystem

A series is suggested if no 'skip' rule matches, every 'need' rule matches
and its score is at least min_score.

### Fixed keys

| Key | Default | Meaning |
|---|---|---|
| limit | 10 | Most series to suggest |
| min_score | 1 | Lowest score to suggest |
| days | 30 | How far back to look, as the inbox does |

These cannot be used as rule names.

### Ranges

Ranges use the patch-selection syntax from 'review -i' (#219): '25-' is 25
or more, '-3' is 3 or fewer, '3-10' is 3 to 10 and '5' is exactly 5. This
needs parse_patch_selection() split into a parser for one range which does
not build a set, since '25-' has no end.

### Projects and repos

- The project's section, e.g. [linux_suggest], is merged with [suggest]
  by the existing _ProjectConfigParser.items(): a rule in the project's
  section replaces one of the same name, and 'off' removes it
- A repo's .patman can add or replace rules for that repo, as
  ~/dev/dtc/.patman does for [maint]

## 2. Tests

Each test has a cost, which sets the order they run in (see section 3).

| Test | Matches when | Data | Cost |
|---|---|---|---|
| `prefix P...` | a patch subject starts with one of these, after any [PATCH vN] | series list | free |
| `from GLOB...` | the submitter's address or name matches | series list | free |
| `patches RANGE` | the number of patches | series list | free |
| `version RANGE` | the version | series list | free |
| `age RANGE` | days since it was sent | series list | free |
| `rfc` | the series is an RFC | series list | free |
| `reviewed-before` | you reviewed an earlier version | review DB (the inbox's 'earlier') | free |
| `state S...` | some patch is in one of these patchwork states | cached states (get_states()) | free |
| `path GLOB...` | a patch touches a matching file | diffstat of each patch | per patch |
| `only-path GLOB...` | every file touched matches | diffstat | per patch |
| `lines RANGE` | lines added and removed, in total | diffstat | per patch |
| `maintainer [M] [R]` | you are M: or R: in MAINTAINERS for a file touched | diffstat, MAINTAINERS in your repo | per patch |
| `word REGEX...` | the diff adds or removes a matching line | diff | per patch |
| `has TAG...` | some patch has one of these tags, e.g. reviewed-by | patch content and comments | per patch |
| `to-me` | you are in To: or Cc: of some patch | patch headers | per patch |
| `no-replies RANGE` | days since it was sent, with no comments from anyone but the submitter | comments | per patch |
| `fixes` | a Fixes: tag names one of your commits | patch content, git log in your repo | per patch |
| `delegate me\|none` | the patches' patchwork delegate | patch detail | per patch |
| `checks pass\|fail` | patchwork's CI result | checks for each patch | per patch, extra request |
| `blame` | it changes lines you wrote | diff, git blame in your repo | per patch, slow locally |
| `newcomer` | the submitter has no earlier series | a search by submitter | extra request |

Notes:

- 'You' is your own addresses, as for lookup.is_own_series(), plus the
  git user's email
- Globs use fnmatch, with '**' matching across '/'
- 'path', 'only-path', 'lines', 'maintainer' and 'word' all need only the
  patch itself, which never changes, so it is cached for good in pwcache
  by patch ID
- 'blame' runs git blame on just the lines each hunk removes or changes,
  against the series' base if the repo has it, else upstream
- 'newcomer' could first look for the submitter in the series lists
  already read for the inbox, and the review DB, before asking patchwork
- On lore (dtc, the Linux server) there are no states, delegates or
  checks, so those tests never match there, and the settings check warns
  about them

## 3. Running the rules

1. Read the recent series as the inbox and the scan do (review/scan.py's
   SCAN_DAYS and SCAN_PAGE, and the cached pages), limited to 'days'
2. Leave out your own series, and those you are already reviewing or have
   reviewed at this version
3. Run the free tests on every series. Apply 'skip' and 'need' rules which
   are free
4. Work out the highest score each series could still reach from the
   rules not yet run. Drop those which cannot reach min_score, or cannot
   get into the top 'limit' given the scores already settled
5. Run the per-patch tests on the rest, fetching each patch once and
   sharing it between tests, then the extra-request tests last, with the
   same pruning between each step

With step 4, a rule set made only of free tests makes no requests beyond
those of the inbox, and a typical one fetches the patches of only the
few dozen series still in the running.

## 4. Commands

```
$ patman review --suggest
  9  i2c: bcm2835: Add new BCM2835/BCM2711 I2C driver (v2, 1 patch)
       https://patchwork.ozlabs.org/project/uboot/list/?series=530318&state=*&archive=both
       i2c +3, listed +3, newcomer +2, cc-me +1
  5  boot: Fix the order of bootflows (v3, 4 patches)
       https://patchwork.ozlabs.org/project/uboot/list/?series=530512&state=*&archive=both
       boot +2, again +3
  3  dm: core: Add ofnode_read_u64_array() (v1, 2 patches)
       https://patchwork.ozlabs.org/project/uboot/list/?series=530601&state=*&archive=both
       dm +3
3 of 112 recent series suggested
```

Each suggestion gives the link to the series from get_series_url(), which
is its page on patchwork, or its thread on lore for a project which uses
lore (lorelist.LoreList), so that it can be opened from the terminal.

- `--suggest`: list the suggestions, using the rules from the settings
- `--explain SERIES`: show each rule's result for one series, with what it
  matched (the files, tags or lines), including the rules not reached
- `--limit N`: suggest at most N, overriding 'limit' ('-n' is taken by
  --dry-run)
- `--suggest-rules`: check the rules and print them as understood, after
  merging the project's and the repo's sections, with any warnings

A series is given as for 'review -s', so `--explain 530318` works.

In 'patman serve', the inbox gets a 'Suggested' group at the top, showing
each series' score and reasons on its card, with the same Review button.
The suggestions are worked out when the inbox is read, and cached with
it, so a reload does not run the costly tests again.

## Suggested PRs

1. Settings: read [suggest], parse the rules and ranges, with
   --suggest-rules and tests for the parsing, merging and warnings
2. The free tests, scoring and --suggest, with fixtures from a saved
   series list, making no requests
3. The per-patch tests, the pruning and the pwcache caching of patches,
   with a test counting the requests made
4. blame, fixes and newcomer, and --explain
5. The 'Suggested' group in the inbox, with its reasons on each card

## Decisions

Made 2026-11-01:

- The command is 'patman review --suggest'
- 'need' stays: it is the simple way to see only series in your area
- The patchwork tests (state, delegate, checks) stay, for people who want
  them. They make requests only when a rule uses them, and only for the
  series still in the running, so a rule set without them costs nothing
  extra
- A project's section adds to [suggest]: a rule there replaces the general
  one of the same name and 'off' removes it, as [linux_settings] works

Open:

- The inbox group, and any other UI, to be talked about later
