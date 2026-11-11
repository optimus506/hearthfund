"""The deny-list scanner: what counts as personal and what the app is allowed to spell out."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'privacy_scan.py'
spec = importlib.util.spec_from_file_location('privacy_scan', SCRIPT)
assert spec and spec.loader
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def hits(private: Path, text: str) -> list[tuple[str, str]]:
    sc = ps.Scanner(ps.build_denylist([private]))
    sc.scan('f', text)
    return [(kind, value) for _, kind, value in sc.hits]


def test_payees_amounts_and_suffixes_are_found_but_not_the_apps_own_words(tmp_path: Path) -> None:
    (tmp_path / 'bank.csv').write_text('Date,Description,Amount\n'
                                       '01/02/2029,PAYPAL ZORBLATT REFUND ACCT 0009 CARD 4321,-123.45\n')
    assert hits(tmp_path, 'paid Zorblatt 123.45 from the card ending 4321') == [
        ('name', 'Zor*****'), ('amount', '123***'), ('suffix', '432*')]
    assert hits(tmp_path, 'cents 12345') == [('amount-cents', '123**')]
    # payment services and everyday words are not personal; nor is a suffix that equals a migration number
    assert '0009' in ps.migration_numbers()
    assert hits(tmp_path, 'PayPal refund; migration 0009 runs on start') == []


def test_generic_patterns_run_without_a_private_folder() -> None:
    sc = ps.Scanner(None)
    # put together here so this file itself passes the scan
    card, address = '4111' + '1' * 12, 'someone@' + 'real-domain.io'
    sc.scan('f', 'write to %s or someone@example.com; card %s' % (address, card))
    assert [kind for _, kind, _ in sc.hits] == ['card-number', 'email']


def test_public_lists_are_checked_for_numbers_but_not_names(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / 'bank.csv').write_text('Date,Description,Amount\n01/02/2029,ZORBLATT STORES,-123.45\n')
    repo = tmp_path / 'repo'
    (repo / 'examples').mkdir(parents=True)
    (repo / 'examples' / 'starter-rules.yaml').write_text('contains: [ZORBLATT]\nmax_amount: 123.45\n')
    (repo / 'notes.md').write_text('we shop at Zorblatt\n')
    monkeypatch.setattr(ps, 'ROOT', repo)
    sc = ps.Scanner(ps.build_denylist([tmp_path]))
    ps.scan_repo(sc)
    assert sorted((where, kind) for where, kind, _ in sc.hits) == [
        ('examples/starter-rules.yaml:2', 'amount'), ('notes.md:1', 'name')]
