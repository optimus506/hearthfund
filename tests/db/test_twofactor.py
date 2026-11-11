"""Two-factor sign-in in the database: enrolment, codes used once, recovery codes, the challenge (D-095)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from hbudget.core import totp
from hbudget.db import twofactor, users

T = datetime(2029, 5, 1, 12, 0, 10, tzinfo=UTC)
S = 'the-session-that-gave-the-password'


def code(secret: str, at: datetime) -> str:
    return totp.code_at(secret, at.timestamp())


@pytest.fixture
def uid(conn: sqlite3.Connection) -> int:
    return users.create_user(conn, 'sam', 'a long password')


def enrol(conn: sqlite3.Connection, uid: int) -> tuple[str, list[str]]:
    secret = twofactor.begin(conn, uid, session=S, now=T)
    codes = twofactor.confirm(conn, uid, code(secret, T), session=S, now=T)
    assert codes is not None
    return secret, codes


def test_enrolment_needs_a_right_code_and_signs_out_other_sessions(conn: sqlite3.Connection, uid: int) -> None:
    assert twofactor.state(conn, uid) == twofactor.State(False, None, None, 0)
    keep, _ = users.create_session(conn, uid)
    other, _ = users.create_session(conn, uid)
    secret = twofactor.begin(conn, uid, session=keep, now=T)
    assert len(secret) == 32 and twofactor.state(conn, uid, session=keep, now=T).pending_secret == secret
    assert not users.get_user(conn, uid).two_factor                       # type: ignore[union-attr]
    assert twofactor.check_code(conn, uid, code(secret, T), now=T) is None   # pending is not on
    assert twofactor.confirm(conn, uid, '000000', session=keep, now=T) is None
    assert twofactor.begin(conn, uid, session=keep, now=T) != secret      # starting again replaces the secret
    secret = twofactor.state(conn, uid, session=keep, now=T).pending_secret or ''
    codes = twofactor.confirm(conn, uid, code(secret, T), session=keep, now=T)
    assert codes and len(codes) == twofactor.RECOVERY_CODES == len(set(codes))
    st = twofactor.state(conn, uid, session=keep, now=T)
    assert st.enabled and st.pending_secret is None and st.codes_left == 10
    assert users.get_user(conn, uid).two_factor                           # type: ignore[union-attr]
    assert users.get_session(conn, keep) is not None and users.get_session(conn, other) is None
    # only hashes are stored
    stored = [r[0] for r in conn.execute('SELECT code_hash FROM recovery_codes')]
    assert not set(stored) & set(codes) and all(len(h) == 64 for h in stored)
    with pytest.raises(ValueError):
        twofactor.begin(conn, uid, session=keep, now=T)
    assert twofactor.confirm(conn, uid, code(secret, T + timedelta(seconds=30)), session=keep, now=T) is None  # on


def test_a_pending_key_belongs_to_the_session_that_started_it_and_runs_out(conn: sqlite3.Connection, uid: int) -> None:
    """Another session of the same user (a borrowed one) never sees the key and cannot confirm it."""
    secret = twofactor.begin(conn, uid, session=S, now=T)
    for session in (None, 'another-session'):
        assert twofactor.state(conn, uid, session=session, now=T).pending_secret is None
    assert twofactor.confirm(conn, uid, code(secret, T), session='another-session', now=T) is None
    late = T + timedelta(minutes=twofactor.PENDING_MINUTES)
    assert twofactor.state(conn, uid, session=S, now=late - timedelta(seconds=1)).pending_secret == secret
    assert twofactor.state(conn, uid, session=S, now=late).pending_secret is None
    assert twofactor.confirm(conn, uid, code(secret, late), session=S, now=late) is None
    assert not twofactor.state(conn, uid).enabled
    # cancelling forgets a pending key and never turns off two-factor sign-in that is on
    assert twofactor.cancel(conn, uid) is True and twofactor.cancel(conn, uid) is False
    assert conn.execute('SELECT totp_secret FROM users WHERE id = ?', (uid,)).fetchone()[0] is None
    enrol(conn, uid)
    assert twofactor.cancel(conn, uid) is False and twofactor.state(conn, uid).enabled


def test_a_code_works_once_and_a_recovery_code_once(conn: sqlite3.Connection, uid: int) -> None:
    secret, codes = enrol(conn, uid)
    assert twofactor.check_code(conn, uid, code(secret, T), now=T) is None        # the enrolment code, replayed
    nxt = T + timedelta(seconds=30)
    assert twofactor.check_code(conn, uid, code(secret, nxt), now=nxt) == 'totp'
    assert twofactor.check_code(conn, uid, code(secret, nxt), now=nxt) is None    # replay
    assert twofactor.check_code(conn, uid, code(secret, T), now=nxt) is None      # an older step
    later = T + timedelta(minutes=5)
    assert twofactor.check_code(conn, uid, code(secret, later - timedelta(seconds=30)), now=later) == 'totp'   # skew
    assert twofactor.check_code(conn, uid, code(secret, later + timedelta(seconds=90)), now=later) is None
    # recovery codes: as typed (upper case, no dashes), once each, not where only the app's code will do
    assert twofactor.check_code(conn, uid, codes[0], recovery=False, now=later) is None
    assert twofactor.check_code(conn, uid, codes[0].upper().replace('-', ' '), now=later) == 'recovery'
    assert twofactor.check_code(conn, uid, codes[0], now=later) is None
    assert twofactor.state(conn, uid).codes_left == 9
    assert twofactor.check_code(conn, uid, 'aaaa-bbbb-cccc', now=later) is None
    fresh = twofactor.new_recovery_codes(conn, uid)
    assert twofactor.state(conn, uid).codes_left == 10 and not set(fresh) & set(codes)
    assert twofactor.check_code(conn, uid, codes[1], now=later) is None           # the old ones are gone
    assert twofactor.check_code(conn, uid, fresh[1], now=later) == 'recovery'


def test_disable_forgets_everything(conn: sqlite3.Connection, uid: int) -> None:
    _, codes = enrol(conn, uid)
    token, _ = users.create_session(conn, uid)
    challenge = twofactor.new_challenge(conn, uid, now=T)
    assert twofactor.disable(conn, uid) is True
    assert twofactor.state(conn, uid) == twofactor.State(False, None, None, 0)
    assert users.get_session(conn, token) is None and twofactor.challenge_user(conn, challenge, now=T) is None
    assert conn.execute('SELECT COUNT(*) FROM recovery_codes').fetchone()[0] == 0
    assert twofactor.check_code(conn, uid, codes[0], now=T) is None
    assert twofactor.disable(conn, uid) is False
    with pytest.raises(ValueError):
        twofactor.new_recovery_codes(conn, uid)
    # a pending enrolment is not "on": disable leaves it (and the sessions) alone
    token, _ = users.create_session(conn, uid)
    twofactor.begin(conn, uid, session=token, now=T)
    assert twofactor.disable(conn, uid) is False and users.get_session(conn, token) is not None


def test_a_new_password_or_a_disabled_account_ends_a_half_done_sign_in(conn: sqlite3.Connection, uid: int) -> None:
    enrol(conn, uid)
    challenge = twofactor.new_challenge(conn, uid, now=T)
    users.set_password(conn, uid, 'another long password')
    assert twofactor.challenge_user(conn, challenge, now=T) is None
    challenge = twofactor.new_challenge(conn, uid, now=T)
    users.set_disabled(conn, uid, True)
    users.set_disabled(conn, uid, False)
    assert twofactor.challenge_user(conn, challenge, now=T) is None


def test_challenge_is_short_lived_single_and_only_for_two_factor_users(conn: sqlite3.Connection, uid: int) -> None:
    enrol(conn, uid)
    first = twofactor.new_challenge(conn, uid, now=T)
    assert twofactor.challenge_user(conn, first, now=T) == uid
    assert twofactor.challenge_user(conn, first, now=T + timedelta(minutes=5)) is None     # expired
    assert twofactor.challenge_user(conn, 'not-a-token', now=T) is None
    assert twofactor.challenge_user(conn, None, now=T) is None and twofactor.challenge_user(conn, '', now=T) is None
    second = twofactor.new_challenge(conn, uid, now=T)                                     # replaces the first
    assert twofactor.challenge_user(conn, first, now=T) is None
    assert conn.execute('SELECT token_hash FROM login_challenges').fetchone()[0] != second     # stored hashed
    users.set_disabled(conn, uid, True)
    assert twofactor.challenge_user(conn, second, now=T) is None
    users.set_disabled(conn, uid, False)
    twofactor.drop_challenge(conn, second)
    assert twofactor.challenge_user(conn, second, now=T) is None
    third = twofactor.new_challenge(conn, uid, now=T)
    assert users.purge(conn, now=T + timedelta(hours=1))['sessions'] == 0
    assert conn.execute('SELECT COUNT(*) FROM login_challenges').fetchone()[0] == 0   # expired ones go
    assert twofactor.challenge_user(conn, third, now=T) is None
    # the challenge is not a session
    assert users.get_session(conn, twofactor.new_challenge(conn, uid, now=T)) is None


def test_wrong_codes_lock_the_app_codes_for_a_day_and_recovery_codes_still_work(conn: sqlite3.Connection,
                                                                                uid: int) -> None:
    """Someone who has the password gets CODE_FAILURES guesses a day, not one a minute (D-097)."""
    secret, codes = enrol(conn, uid)
    t = T + timedelta(minutes=5)
    wrong = '000000' if code(secret, t) != '000000' else '111111'
    for k in range(twofactor.CODE_FAILURES - 1):
        assert twofactor.check(conn, uid, wrong, now=t + timedelta(seconds=k)) is None
    assert not twofactor.codes_locked(conn, uid, now=t)
    assert twofactor.check(conn, uid, wrong, now=t) is None
    assert twofactor.codes_locked(conn, uid, now=t) and twofactor.failures(conn, uid, now=t) == twofactor.CODE_FAILURES
    assert twofactor.check(conn, uid, code(secret, t), now=t) is None              # a right code is refused too
    assert twofactor.failures(conn, uid, now=t) == twofactor.CODE_FAILURES          # and not counted again
    assert twofactor.check(conn, uid, codes[0], now=t) == 'recovery'               # recovery codes still work
    assert twofactor.check(conn, uid, codes[1], recovery=False, now=t) is None
    later = t + twofactor.CODE_WINDOW + timedelta(seconds=30)
    assert not twofactor.codes_locked(conn, uid, now=later)
    assert twofactor.check(conn, uid, code(secret, later), now=later) == 'totp'
    assert twofactor.failures(conn, uid, now=later) == 0                           # a right code clears them
    # turning it off forgets them
    twofactor.check(conn, uid, wrong, now=later)
    twofactor.disable(conn, uid)
    assert conn.execute('SELECT COUNT(*) FROM code_failures').fetchone()[0] == 0


def test_a_code_used_a_moment_ago_is_reused_not_wrong(conn: sqlite3.Connection, uid: int) -> None:
    secret, _ = enrol(conn, uid)
    t = T + timedelta(minutes=5)
    assert twofactor.check(conn, uid, code(secret, t), now=t) == 'totp'
    assert twofactor.check(conn, uid, code(secret, t), now=t) == twofactor.REUSED
    assert twofactor.check_code(conn, uid, code(secret, t), now=t) is None
    assert twofactor.failures(conn, uid, now=t) == 0
