"""One-time codes (RFC 6238) and the otpauth link (D-095)."""
import base64

import pytest

from hbudget.core import totp

# RFC 6238 appendix B, SHA-1: the key is the ASCII text below; the 8-digit values there end in these 6 digits
KEY = base64.b32encode(b'12345678901234567890').decode()
RFC = [(59, '287082'), (1111111109, '081804'), (1111111111, '050471'), (1234567890, '005924'),
       (2000000000, '279037'), (20000000000, '353130')]


@pytest.mark.parametrize('at,code', RFC)
def test_rfc_6238_vectors(at: int, code: str) -> None:
    assert totp.code_at(KEY, at) == code
    assert totp.verify(KEY, code, at) == at // 30


def test_rfc_4226_counter_values() -> None:
    assert [totp.hotp(b'12345678901234567890', i) for i in range(4)] == ['755224', '287082', '359152', '969429']


def test_window_replay_and_bad_input() -> None:
    t = 1_900_000_000
    step = t // 30
    now = totp.code_at(KEY, t)
    # one step either side is accepted (a clock a little off), two is not
    assert totp.verify(KEY, totp.code_at(KEY, t - 30), t) == step - 1
    assert totp.verify(KEY, totp.code_at(KEY, t + 30), t) == step + 1
    assert totp.verify(KEY, totp.code_at(KEY, t - 60), t) is None
    assert totp.verify(KEY, totp.code_at(KEY, t + 60), t) is None
    # a step already accepted, and anything before it, is refused; the next step still works
    assert totp.verify(KEY, now, t, after_step=step) is None
    assert totp.verify(KEY, totp.code_at(KEY, t - 30), t, after_step=step) is None
    assert totp.verify(KEY, totp.code_at(KEY, t + 30), t, after_step=step) == step + 1
    assert totp.verify(KEY, now, t, after_step=step - 1) == step
    # spaces are fine (apps show "123 456"); anything that is not six digits is not a code
    assert totp.verify(KEY, now[:3] + ' ' + now[3:], t) == step
    for bad in ('', '12345', '1234567', 'abcdef', now + '0', '-' + now[1:]):
        assert totp.verify(KEY, bad, t) is None
    # the secret may be shown in groups and lower case
    spaced = ' '.join(KEY[i:i + 4] for i in range(0, len(KEY), 4)).lower()
    assert totp.code_at(spaced, t) == now


def test_uri() -> None:
    assert totp.uri('abcd efgh', 'sam smith', 'My App') == (
        'otpauth://totp/My%20App%3Asam%20smith?secret=ABCDEFGH&issuer=My%20App&algorithm=SHA1&digits=6&period=30')


def test_digits_that_are_not_ascii_are_just_wrong() -> None:
    for odd in ('١٢٣٤٥٦', '²²²²²²', '１２３４５６'):     # str.isdigit() says yes to all of these
        assert totp.verify(base64.b32encode(b'12345678901234567890').decode(), odd, 59) is None


def test_decode_secret_takes_the_key_as_apps_show_it() -> None:
    raw = b'12345678901234567890'
    shown = base64.b32encode(raw).decode()
    spaced = ' '.join(shown[i:i + 4] for i in range(0, len(shown), 4)).lower()
    assert totp.decode_secret(shown) == raw and totp.decode_secret(spaced) == raw
    with pytest.raises(ValueError):
        totp.decode_secret('not base32 at all!')
