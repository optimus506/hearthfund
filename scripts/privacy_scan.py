#!/usr/bin/env python3
"""Scan the repo, its git history and (optionally) a Docker image for personal data.

  BUDGET_PRIVATE_DIR=~/private python3 scripts/privacy_scan.py --repo --history [--image NAME]

The deny-list is built in memory at run time from the private files in BUDGET_PRIVATE_DIR
(and BUDGET_PRIVATE_EXTRA, a colon-separated list of more folders with bank CSVs). It is never
written to disk, and hits are printed masked (first 3 characters only).

Without BUDGET_PRIVATE_DIR only the generic patterns run (card/account numbers, emails, phone
numbers), which is what CI does.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', '.mypy_cache', '.ruff_cache', '.pytest_cache',
             'private_tests', 'data', 'htmlcov', 'dist', 'build'}
TEXT_EXT = {'.py', '.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.cfg', '.ini', '.html', '.js', '.css', '.csv',
            '.ofx', '.qfx', '.sql', '.sh', '.env', '.example', '', '.svg', '.j2', '.jinja', '.xml'}

# Generic budget words. They are never treated as personal, even when a payee uses them.
# Only everyday words belong here, never fragments of real payee or merchant names.
GENERIC = set('''
a ach amount an and annual at auto autopay back balance bank banking bill bills birthday board bonus budget
budgeted budgets by cable car card cards care carryover cash cashback category charges charts check checkbook
checking childcare christmas colour contributing credit creditor creditors csrf data date dates daycare debit debt debts
disb shop
dental deploy deposit description diner dining downloads earned eating effective electric electricity entertainment
everyday expenses extended external family fee fees food foods for fridays from fuel fund gas gift gifts
grill groceries grocery gym health hoa holiday home id in income insurance interest internet is it lawn left
list loan loans market maximum medical membership memo minimum misc mobile mondays money monthly mortgage
notes number of on online or other out over paid pay paycheck payday paydays payment payments payout payroll
pending personal pet pets pharmacy phone pizza posted posting power projection projections purchase purchases
reconciled reconciliation reference refund remote rent restaurant restaurants reviews savings school seasonal
service services sewer shopping spending station status streaming student subscription subscriptions tax
taxes the to total transaction transactions transfer transfers trash tuition tv type updated utilities
utility vacation vet water web weekend weekly with withdrawal yearly
'''.split())  # noqa: SIM905

AMOUNT_RE = re.compile(r'(?<![\d.])\$?\(?-?\d{1,3}(?:,?\d{3})*\.\d{2}\)?(?![\d])')
EMAIL_RE = re.compile(r'(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE_RE = re.compile(r'(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)')
HEX_RE = re.compile(r'(?<![0-9A-Za-z])(?=[0-9]*[a-fA-F])[0-9a-fA-F]{32,}(?![0-9A-Za-z])')  # must contain a hex letter
LONGNUM_RE = re.compile(r'(?<![\d.])\d{12,19}(?![\d.])')
DOLLARS_RE = re.compile(r'\$(\d{1,3}(?:,\d{3})+|\d{4})(?![\d,])|(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d{4})\.00\b')
CONSTANT_CENTS = {3044}   # 30.44 = average days per month
TIME_CONSTANTS = {'86400', '604800', '3600000', '86400000', '604800000'}   # seconds/ms per day or week
SUFFIX_RE = re.compile(r'(?i)\b(?:loan|card|acct|account|ending|share|x)\b[\s#*:-]*\S{0,6}?(\d{4})\b')
ALLOWED_EMAIL_DOMAINS = ('example.com', 'example.org', 'example.net', 'example.invalid', 'noreply.github.com',
                         'anthropic.com', 'localhost')
# Exact service addresses: GitHub's own committer on PR merge commits (CI checks those out) and web merges, and
# the Signed-off-by line of Dependabot's commits (its branches are in the history CI scans).
ALLOWED_EMAILS = {'noreply@github.com', 'support@github.com'}

ODS_NS = {'t': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0',
          'o': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
          'x': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'}


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def norm_amount(text: str) -> str | None:
    s = text.replace('$', '').replace(',', '').replace('(', '').replace(')', '').lstrip('-')
    try:
        v = float(s)
    except ValueError:
        return None
    cents = round(v * 100)
    if abs(cents) < 1000 or cents % 100 == 0:   # under $10 or whole dollars: too generic to be personal
        return None
    if abs(cents) in CONSTANT_CENTS:
        return None
    return '%.2f' % (abs(cents) / 100)


class DenyList:
    def __init__(self) -> None:
        self.words: set[str] = set()
        self.amounts: set[str] = set()
        self.emails: set[str] = set()
        self.numbers: set[str] = set()
        self.cents: set[str] = set()      # non-round amounts written as integer cents, e.g. 12345 for 123.45
        self.suffixes: set[str] = set()   # 4-digit account / card suffixes seen next to LOAN, CARD, ACCT...
        self.whole_dollars: set[str] = set()
        self.payee_words: set[str] = set()
        self.review: set[str] = set()

    def add_payee(self, text: str) -> None:
        self.add_name(text)
        # only ALL-CAPS tokens are payee keywords (bank descriptions are upper case)
        self.payee_words.update(w.lower() for w in re.findall(r'\b[A-Z]{4,}\b', text))

    def add_name(self, text: str) -> None:
        for raw in re.split(r'[^A-Za-z0-9&\']+', text):
            w = raw.strip("'").lower()
            if len(w) < 4 or w in GENERIC or w.isdigit():
                continue
            self.words.add(w)
        for m in EMAIL_RE.finditer(text):
            self.emails.add(m.group().lower())

    def add_amount(self, value: str) -> None:
        a = norm_amount(value)
        if a:
            self.amounts.add(a)
            if float(a) >= 50:   # smaller cents values collide with ordinary constants
                self.cents.add(str(round(float(a) * 100)))

    def add_text(self, text: str) -> None:
        for m in AMOUNT_RE.finditer(text):
            self.add_amount(m.group())
        for m in EMAIL_RE.finditer(text):
            self.emails.add(m.group().lower())
        for m in re.finditer(r'\d{6,}', text):
            self.numbers.add(m.group())
        for m in SUFFIX_RE.finditer(text):
            n = m.group(1)
            if not 1900 <= int(n) <= 2099 and n != '0000':
                self.suffixes.add(n)
        for m in DOLLARS_RE.finditer(text):
            self.whole_dollars.add((m.group(1) or m.group(2)).replace(',', ''))

    def __len__(self) -> int:
        return len(self.words) + len(self.amounts) + len(self.emails) + len(self.numbers)


def load_ods(path: Path, dl: DenyList) -> None:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read('content.xml'))
    for cell in root.iter('{{{}}}table-cell'.format(ODS_NS['t'])):
        v = cell.get('{{{}}}value'.format(ODS_NS['o']))
        if v:
            dl.add_amount(v)
        text = ''.join(cell.itertext()).strip()
        if text and not re.fullmatch(r'[\d$,.()\- %]+', text):
            dl.add_name(text)
        if text:
            dl.add_text(text)


def load_csv(path: Path, dl: DenyList) -> None:
    with open(path, newline='', encoding='utf-8-sig', errors='replace') as f:
        for row in csv.reader(f):
            for cell in row:
                dl.add_text(cell)
                if re.search(r'[A-Za-z]{4,}', cell):
                    dl.add_name(cell)


def load_py(path: Path, dl: DenyList) -> None:
    text = path.read_text(errors='replace')
    dl.add_text(text)
    for m in re.finditer(r"'([^'\n]{3,60})'|\"([^\"\n]{3,60})\"", text):
        s = m.group(1) or m.group(2)
        if re.search(r'[A-Z]{3,}|\b[A-Z][a-z]+ [A-Z][a-z]+', s):   # payee keywords and proper names
            dl.add_payee(s)


def load_md(path: Path, dl: DenyList) -> None:
    text = path.read_text(errors='replace')
    dl.add_text(text)
    # payee-like tokens: all-caps words and names in the matching tables
    for m in re.finditer(r'\b[A-Z][A-Z0-9&]{3,}\b', text):
        dl.add_name(m.group())
    for line in text.splitlines():
        if line.startswith('|'):
            dl.add_name(line.split('|')[1])


TECH = set('''
appendix artifact intuit artifactdata budgetlib claude code database docker drive explorer export false fastapi
fixtures flow gdrive google headless history html http https import inbox json libreoffice linux lxml
markdown none null page pandas plaid plan python python3 rclone readme reconcile report reports rules sankey
section sheet sheets snapshot soffice sqlite sync tab tabs tests text todo true upload uploads verify
versions vlookup windows wsl yaml
'''.split())  # noqa: SIM905


# Words the app's own code has to spell out: payment services whose bank lines stand for many merchants, and the
# abbreviations banks print on bill payments. Nearly every household's bank file has them, so they identify nobody.
# Nothing else belongs here: a shop, lender, employer or utility is a payee and stays on the deny-list.
SERVICES = set('amazon amzn epayment paymt paypal venmo zelle'.split())  # noqa: SIM905


# Public lists of large national companies (the starter payee rules). They are written from general knowledge,
# not from anyone's bank data, and any household's payees overlap with them, so names are not reported there.
# Amounts, numbers, suffixes and e-mail addresses in these files are still checked.
PUBLIC_LISTS = ('examples/starter-rules.yaml', 'hbudget/schema/starter-rules.yaml')


def common_words() -> set[str]:
    """Ordinary English words, taken from the Python standard library's own source and docs.
    A word found there is not personal by itself (real names and payees are not in it)."""
    words: set[str] = set()
    stdlib = Path(sysconfig.get_paths()['stdlib'])
    for p in stdlib.rglob('*.py'):
        try:
            words.update(w.lower() for w in re.findall(r'[A-Za-z]{4,}', p.read_text(errors='replace')))
        except OSError:
            continue
    return words


def migration_numbers() -> set[str]:
    """The app's own migration numbers ('0009'): file names, tests and the changelog must spell them out, so a
    private account suffix that happens to equal one cannot be told apart and is not reported."""
    folder = ROOT / 'src' / 'hbudget' / 'db' / 'migrations'
    return {p.name[:4] for p in folder.glob('[0-9][0-9][0-9][0-9]_*.sql')}


def build_denylist(dirs: list[Path]) -> DenyList:
    dl = DenyList()
    for d in dirs:
        for p in sorted(d.rglob('*')):
            if not p.is_file() or p.stat().st_size > 20_000_000:
                continue
            suf = p.suffix.lower()
            try:
                if suf == '.ods':
                    load_ods(p, dl)
                elif suf == '.csv':
                    load_csv(p, dl)
                elif suf == '.py':
                    load_py(p, dl)
                elif suf == '.md':
                    load_md(p, dl)
                elif suf == '.html':
                    dl.add_text(p.read_text(errors='replace'))
            except (OSError, zipfile.BadZipFile, ET.ParseError, UnicodeError):
                print(f'warning: could not read a private file ({p.suffix})', file=sys.stderr)
    dl.words -= TECH | SERVICES
    dl.suffixes -= dl.whole_dollars   # a 4-digit number that is also a dollar amount is an amount, not a suffix
    dl.suffixes -= migration_numbers()
    dl.numbers = {n for n in dl.numbers if not re.fullmatch(r'\d0+', n) and n not in TIME_CONSTANTS}
    # ordinary words come off the strong list; payee keywords that are also ordinary words are kept as
    # "review" items (case-sensitive, so only the capitalised brand spelling is reported)
    common = common_words()
    dl.review = {w for w in dl.words if w in common and w in dl.payee_words and w not in TECH}
    dl.words = {w for w in dl.words if w not in common and not re.fullmatch(r'\d+(st|nd|rd|th)', w)}
    return dl


def mask(s: str) -> str:
    return s[:3] + '*' * max(0, len(s) - 3)


class Scanner:
    def __init__(self, dl: DenyList | None) -> None:
        self.dl = dl
        self.hits: list[tuple[str, str, str]] = []
        if dl and dl.words:
            alt = '|'.join(sorted((re.escape(w) for w in dl.words), key=len, reverse=True))
            self.word_re: re.Pattern[str] | None = re.compile(rf'(?<![A-Za-z0-9])({alt})(?![A-Za-z0-9])', re.I)
        else:
            self.word_re = None
        review = sorted(getattr(dl, 'review', set()) or [])
        forms = [f for w in review for f in (w.upper(), w.capitalize())]
        alt_review = '|'.join(map(re.escape, forms))
        self.review_re = re.compile(r'(?<![A-Za-z0-9])(%s)(?![A-Za-z0-9])' % alt_review) if forms else None

    def scan(self, where: str, text: str, *, review: bool = True, names: bool = True) -> None:
        for lineno, raw in enumerate(text.splitlines(), 1):
            loc = '%s:%d' % (where, lineno)
            # long hex strings (sha256 pins in lock files, git ids) are not numbers anyone owns
            line = HEX_RE.sub(' ', raw)
            for m in LONGNUM_RE.finditer(line):
                if luhn_ok(m.group()):
                    self.hits.append((loc, 'card-number', mask(m.group())))
            for m in EMAIL_RE.finditer(line):
                e = m.group().lower()
                if (e not in ALLOWED_EMAILS and not e.endswith(ALLOWED_EMAIL_DOMAINS)
                        and not e.split('@')[1].startswith('example')):
                    self.hits.append((loc, 'email', mask(e)))
            for m in PHONE_RE.finditer(line):
                if not re.search(r'555[ .-]\d{4}', m.group()):
                    self.hits.append((loc, 'phone', mask(m.group())))
            if not self.dl:
                continue
            if names and self.word_re:
                for m in self.word_re.finditer(line):
                    self.hits.append((loc, 'name', mask(m.group())))
            if review and self.review_re:
                for m in self.review_re.finditer(line):
                    self.hits.append((loc, 'review-brand', mask(m.group())))
            for m in AMOUNT_RE.finditer(line):
                a = norm_amount(m.group())
                if a and a in self.dl.amounts:
                    self.hits.append((loc, 'amount', mask(a)))
            for n in re.findall(r'\d{6,}', line):
                if n in self.dl.numbers:
                    self.hits.append((loc, 'number', mask(n)))
            for n in re.findall(r'(?<![\d.])\d{4,7}(?![\d.])', line):
                if n in self.dl.cents:
                    self.hits.append((loc, 'amount-cents', mask(n)))
                elif len(n) == 4 and n in self.dl.suffixes:
                    self.hits.append((loc, 'suffix', mask(n)))


def repo_files() -> list[Path]:
    """Tracked and untracked files, minus what .gitignore excludes (falls back to a walk outside git)."""
    ls = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard'],
                        capture_output=True, text=True, check=False)
    if ls.returncode == 0:
        out = []
        for rel in ls.stdout.splitlines():
            p = ROOT / rel
            suf = p.suffix.lower()
            wanted = suf in TEXT_EXT or suf in ('.xlsx', '.ods') or p.name in ('Dockerfile', 'Makefile', 'LICENSE')
            if p.is_file() and wanted:
                out.append(p)
        return out
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            p = Path(dirpath) / f
            suf = p.suffix.lower()
            if suf in TEXT_EXT or suf in ('.xlsx', '.ods') or p.name in ('Dockerfile', 'Makefile', 'LICENSE'):
                out.append(p)
    return out


def file_text(p: Path) -> str:
    if p.suffix.lower() in ('.xlsx', '.ods'):
        with zipfile.ZipFile(p) as z:
            return '\n'.join(z.read(n).decode('utf-8', 'replace') for n in z.namelist() if n.endswith('.xml'))
    return p.read_text(errors='replace')


def scan_repo(sc: Scanner) -> None:
    for p in repo_files():
        rel = str(p.relative_to(ROOT))
        # the MIT licence text uses ordinary words that some brands also use; names and numbers are still checked
        public = rel.endswith(PUBLIC_LISTS)
        sc.scan(rel, file_text(p), review=rel != 'LICENSE' and not public, names=not public)


def scan_history(sc: Scanner) -> None:
    fmt = '--format=commit %H%nauthor %an <%ae>%ncommitter %cn <%ce>%n%B'
    log = subprocess.run(['git', '-C', str(ROOT), 'log', '--all', '-p', fmt],
                         capture_output=True, text=True, errors='replace', check=False).stdout
    # the diffs of LICENSE and of the public lists get the same exemptions as the files; the rest is scanned in full
    main, licence, public = [], [], []
    part = main
    for line in log.splitlines():
        if line.startswith('diff --git ') or line.startswith('commit '):
            path = line.split(' b/')[0][len('diff --git a/'):] if line.startswith('diff --git a/') else ''
            part = licence if path == 'LICENSE' else public if path.endswith(PUBLIC_LISTS) else main
        part.append(line)
    sc.scan('git-history', '\n'.join(main))
    sc.scan('git-history:LICENSE', '\n'.join(licence), review=False)
    sc.scan('git-history:public-lists', '\n'.join(public), review=False, names=False)


def scan_image(sc: Scanner, image: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        scanned: list[str] = []
        cid = subprocess.run(['docker', 'create', image], capture_output=True, text=True, check=True).stdout.strip()
        try:
            tar_path = os.path.join(tmp, 'fs.tar')
            subprocess.run(['docker', 'export', '-o', tar_path, cid], check=True)
            with tarfile.open(tar_path) as tf:
                for m in tf:
                    if not m.isfile() or m.size > 5_000_000:
                        continue
                    name = m.name
                    # third-party packages and the OS are not ours; scan the app, its data and home folders
                    ours = re.search(r'/site-packages/hbudget/', name) is not None
                    if not ours and not re.match(r'^(app|data|home|root|srv|opt/app)/', name):
                        continue
                    if not ours and ('/site-packages/' in name or '/.venv/' in name):
                        continue
                    if name.endswith(('.pyc', '.so')):
                        continue
                    scanned.append(name)
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    raw = f.read()
                    if name.endswith(('.xlsx', '.ods')):
                        try:
                            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                                raw = b'\n'.join(z.read(n) for n in z.namelist() if n.endswith('.xml'))
                        except zipfile.BadZipFile:
                            pass
                    listed = name.endswith(PUBLIC_LISTS)
                    sc.scan('image:' + name, raw.decode('utf-8', 'replace'), review=not listed, names=not listed)
            inspect = ['docker', 'image', 'inspect', image]
            meta = subprocess.run(inspect, capture_output=True, text=True, check=True).stdout
            sc.scan('image:inspect', meta)
            print('image: scanned %d files (the app package, data and home folders, image metadata)' % len(scanned))
            if not any('/site-packages/hbudget/' in n for n in scanned):
                sc.hits.append(('image', 'setup', 'the app package was not found in the image'))
        finally:
            subprocess.run(['docker', 'rm', cid], capture_output=True, check=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', action='store_true', help='scan the working tree')
    ap.add_argument('--history', action='store_true', help='scan all commits, messages and authors')
    ap.add_argument('--image', help='scan a built Docker image')
    ap.add_argument('--stats', action='store_true', help='print deny-list sizes')
    a = ap.parse_args(argv)
    if not (a.repo or a.history or a.image):
        a.repo = True

    roots = [os.environ.get('BUDGET_PRIVATE_DIR', ''), *os.environ.get('BUDGET_PRIVATE_EXTRA', '').split(':')]
    dirs = [Path(os.path.expanduser(x)) for x in roots if x]
    dirs = [d for d in dirs if d.is_dir()]
    dl = build_denylist(dirs) if dirs else None
    if dl is None:
        print('BUDGET_PRIVATE_DIR not set: generic patterns only.')
    elif a.stats:
        sizes = (len(dl.words), len(dl.amounts), len(dl.emails), len(dl.numbers), len(dl.suffixes))
        print('deny-list: %d words, %d amounts, %d emails, %d numbers, %d suffixes' % sizes)
    sc = Scanner(dl)
    if a.repo:
        scan_repo(sc)
    if a.history:
        scan_history(sc)
    if a.image:
        scan_image(sc, a.image)
    for loc, kind, what in sc.hits:
        print('%-60s %-12s %s' % (loc, kind, what))
    print('%d hit(s)' % len(sc.hits))
    return 1 if sc.hits else 0


if __name__ == '__main__':
    sys.exit(main())
