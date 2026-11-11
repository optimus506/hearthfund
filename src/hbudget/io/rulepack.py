"""Rule packs: ready-made payee rules a household adds in one step (D-094).

A pack is a small YAML or JSON file. The starter pack is `examples/starter-rules.yaml`; the app ships an identical
copy as `hbudget/schema/starter-rules.yaml` (a test keeps them the same). Its rules do
not name a household's bills, because it cannot know them: `apply` links a `pay` rule to every paycheck and a
`bill` rule to the bills whose name contains one of its `bills_named` words. Nothing a household already has is
changed: rules, report groups and card categories are only ever added.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import resources
from typing import Any

from hbudget.core.models import Budget, CardCategory, Direction, FlowGroup, Kind, PayeeRule, RuleKind
from hbudget.core.rules import compile_safe

from . import ImportProblem
from .billsystem import parse

__all__ = [
    'FORMAT',
    'MAX_BYTES',
    'STARTER_PREFIX',
    'Outcome',
    'Pack',
    'PackRule',
    'apply',
    'load',
    'starter',
    'starter_text',
]

FORMAT = 'household-budget/rule-pack'
MAX_BYTES = 64 * 1024
MAX_RULES = 100
STARTER_PREFIX = 'starter-'      # ids of the shipped pack; new rules are added above these (api_household.rule_add)
KINDS = {'pay': RuleKind.PAY, 'bill': RuleKind.BILL, 'track': RuleKind.TRACK, 'ignore': RuleKind.IGNORE}
_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,59}')
_COLOUR = re.compile(r'#[0-9a-fA-F]{6}|--[a-z0-9-]{1,40}')
_TOP = {'format', 'version', 'name', 'rules', 'groups'}
_RULE = {'id', 'name', 'kind', 'contains', 'regex', 'direction', 'bills_named', 'group', 'card_category', 'note'}


@dataclass(frozen=True, slots=True)
class PackRule:
    rule: PayeeRule                       # without targets: apply() finds them
    bills_named: tuple[str, ...] = ()
    group: str = ''
    card_category: str = ''


@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    rules: tuple[PackRule, ...]
    colours: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Outcome:
    budget: Budget
    added: tuple[tuple[PayeeRule, tuple[str, ...]], ...]    # the new rule and the names of the lines it clears
    skipped: tuple[tuple[str, str], ...]                    # rule name, why
    kept: int                                               # rules the household already had (same id)
    groups: tuple[str, ...]                                 # report groups created
    categories: tuple[str, ...]                             # card categories created


def _text(v: object, path: str, errors: list[str], *, limit: int, required: bool = True) -> str:
    if v is None and not required:
        return ''
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        errors.append('%s: text of 1 to %d characters is needed' % (path, limit))
        return ''
    return v.strip()


def _words(v: object, path: str, errors: list[str], *, limit: int = 20) -> tuple[str, ...]:
    if v is None:
        return ()
    if not isinstance(v, list) or len(v) > limit:
        errors.append('%s: a list of at most %d words is needed' % (path, limit))
        return ()
    return tuple(w for i, x in enumerate(v) if (w := _text(x, '%s[%d]' % (path, i), errors, limit=80)))


def _pack_rule(r: Mapping[str, Any], path: str, errors: list[str]) -> PackRule | None:
    n = len(errors)
    for key in sorted(set(r) - _RULE):
        errors.append('%s.%s: not a field of a pack rule' % (path, key))
    rid = r.get('id')
    if not isinstance(rid, str) or not _ID.fullmatch(rid):
        errors.append('%s.id: lower-case letters, digits and "-" are needed (at most 60)' % path)
    kind = KINDS.get(str(r.get('kind')))
    if kind is None:
        errors.append('%s.kind: one of %s' % (path, ', '.join(KINDS)))
    name = _text(r.get('name'), path + '.name', errors, limit=60)
    contains = _words(r.get('contains'), path + '.contains', errors)
    regex = _text(r.get('regex'), path + '.regex', errors, limit=200, required=False) or None
    if regex:
        try:
            compile_safe(regex)
        except ValueError as e:
            errors.append('%s.regex: %s' % (path, e))
    if not contains and not regex:
        errors.append('%s: a rule needs `contains` words or a `regex`' % path)
    direction = str(r.get('direction') or ('in' if kind is RuleKind.PAY else 'any'))
    if direction not in ('in', 'out', 'any'):
        errors.append('%s.direction: in, out or any' % path)
    named = _words(r.get('bills_named'), path + '.bills_named', errors)
    if kind is RuleKind.BILL and not named:
        errors.append('%s.bills_named: a bill rule needs the words to find its bill by' % path)
    if kind is not RuleKind.BILL and named:
        errors.append('%s.bills_named: only bill rules are linked to bills' % path)
    group = _text(r.get('group'), path + '.group', errors, limit=40, required=False)
    category = _text(r.get('card_category'), path + '.card_category', errors, limit=40, required=False)
    if (group or category) and kind is not RuleKind.TRACK:
        errors.append('%s: `group` and `card_category` are for track rules' % path)
    if category and not contains:
        errors.append('%s.card_category: needs `contains` words (card categories have no patterns)' % path)
    note = _text(r.get('note'), path + '.note', errors, limit=300, required=False)
    if len(errors) > n or kind is None:
        return None
    return PackRule(PayeeRule(str(rid), name, kind, contains, regex, Direction(direction), note=note),
                    tuple(w.casefold() for w in named), group, category)


def load(data: bytes, filename: str = '') -> Pack:
    """Parse and check a pack. Every problem is an ImportProblem listing the errors."""
    if len(data) > MAX_BYTES:
        raise ImportProblem('A rule pack is at most %d KB.' % (MAX_BYTES // 1024))
    doc = parse(data, filename)
    errors: list[str] = []
    if doc.get('format') != FORMAT or doc.get('version') != 1:
        raise ImportProblem('This is not a rule pack (it needs `format: %s` and `version: 1`).' % FORMAT)
    for key in sorted(set(doc) - _TOP):
        errors.append('%s: not a field of a rule pack' % key)
    name = _text(doc.get('name'), 'name', errors, limit=80)
    raw = doc.get('rules')
    if not isinstance(raw, list) or not raw or len(raw) > MAX_RULES:
        raise ImportProblem('A rule pack needs `rules`: a list of 1 to %d rules.' % MAX_RULES)
    rules: list[PackRule] = []
    seen: set[str] = set()
    for i, r in enumerate(raw):
        path = 'rules[%d]' % i
        if not isinstance(r, dict):
            errors.append('%s: a rule is a mapping' % path)
            continue
        pr = _pack_rule(r, path, errors)
        if pr is None:
            continue
        if pr.rule.id in seen:
            errors.append('%s.id: "%s" is used twice' % (path, pr.rule.id))
        seen.add(pr.rule.id)
        rules.append(pr)
    colours: list[tuple[str, str]] = []
    groups = doc.get('groups') or {}
    if not isinstance(groups, dict) or len(groups) > 30:
        errors.append('groups: a mapping of group name to colour is needed')
    else:
        for g, c in groups.items():
            if not isinstance(g, str) or not 0 < len(g) <= 40 or not isinstance(c, str) or not _COLOUR.fullmatch(c):
                errors.append('groups: "%s" needs a colour (a theme token such as --f-home, or #rrggbb)' % g)
            else:
                colours.append((g, c))
    if errors:
        shown = errors[:20] + (['... and %d more' % (len(errors) - 20)] if len(errors) > 20 else [])
        raise ImportProblem('The rule pack has problems:\n' + '\n'.join(shown))
    return Pack(name, tuple(rules), tuple(colours))


def starter_text() -> str:
    """The shipped starter pack as text (the same file as examples/starter-rules.yaml)."""
    return resources.files('hbudget').joinpath('schema', 'starter-rules.yaml').read_text(encoding='utf-8')


def starter() -> Pack:
    return load(starter_text().encode(), 'starter-rules.yaml')


def _named(name: str, words: tuple[str, ...]) -> bool:
    text = name.casefold()
    return any(re.search(r'(?<![a-z0-9])%s(?![a-z0-9])' % re.escape(w), text) for w in words)


def apply(budget: Budget, pack: Pack) -> Outcome:
    """Add the pack's rules to the end of the household's rules. Rules, groups and categories the household has
    are never changed; a rule that cannot be linked to a paycheck or a bill is skipped with the reason."""
    have = {r.id for r in budget.rules}
    # bills that already had a rule before this pack; two pack rules may both link one bill (a bill named "Phone &
    # Internet" gets the phone and the internet rule), which is what a combined bill wants
    targeted = {t for r in budget.rules for t in r.targets}
    incomes = tuple(s for s in budget.series if s.kind is Kind.INCOME)
    bills = tuple(s for s in budget.series if s.kind is Kind.BILL)
    names = {s.id: s.name for s in budget.series}
    added: list[tuple[PayeeRule, tuple[str, ...]]] = []
    skipped: list[tuple[str, str]] = []
    kept = 0
    for pr in pack.rules:
        r = pr.rule
        if r.id in have:
            kept += 1
            continue
        targets: tuple[str, ...] = ()
        if r.kind is RuleKind.PAY:
            targets = tuple(s.id for s in incomes)
            if not targets:
                skipped.append((r.name, 'no paycheck to link it to yet'))
                continue
        elif r.kind is RuleKind.BILL:
            found = [s.id for s in bills if _named(s.name, pr.bills_named)]
            if not found:
                skipped.append((r.name, 'no bill with a name like "%s"' % pr.bills_named[0]))
                continue
            targets = tuple(s for s in found if s not in targeted)
            if not targets:
                skipped.append((r.name, 'the bill already has a rule'))
                continue
        added.append((replace(r, targets=targets), tuple(names[t] for t in targets)))
    new_ids = {r.id for r, _ in added}

    by_fold = {g.name.casefold(): i for i, g in enumerate(budget.flow_groups)}
    groups = list(budget.flow_groups)
    colours = {g.casefold(): c for g, c in pack.colours}
    made_groups: list[str] = []
    by_cat = {c.name.casefold() for c in budget.card_categories}
    categories = list(budget.card_categories)
    made_cats: list[str] = []
    for pr in pack.rules:
        if pr.rule.id not in new_ids:
            continue
        if pr.group:
            key = pr.group.casefold()
            if key not in by_fold:
                by_fold[key] = len(groups)
                groups.append(FlowGroup(pr.group, colours.get(key, '--f-other')))
                made_groups.append(pr.group)
            g = groups[by_fold[key]]
            groups[by_fold[key]] = replace(g, rule_ids=(*g.rule_ids, pr.rule.id))
        if pr.card_category and pr.card_category.casefold() not in by_cat:
            by_cat.add(pr.card_category.casefold())
            categories.append(CardCategory(pr.card_category, tuple(k.upper() for k in pr.rule.contains)))
            made_cats.append(pr.card_category)
    nb = replace(budget, rules=(*budget.rules, *(r for r, _ in added)), flow_groups=tuple(groups),
                 card_categories=tuple(categories))
    return Outcome(nb, tuple(added), tuple(skipped), kept, tuple(made_groups), tuple(made_cats))
