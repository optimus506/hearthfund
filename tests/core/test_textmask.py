from hbudget.core.textmask import mask


def test_long_run_keeps_last_four():
    assert mask('CARD 123456789012') == 'CARD ########9012'


def test_five_digit_run_masked():
    assert mask('REF 12345') == 'REF #2345'


def test_four_digits_stay():
    assert mask('TO LOAN ending 1234') == 'TO LOAN ending 1234'


def test_grouped_card_numbers_masked():
    assert mask('PAY 4000 1234 5678 9010 X') == 'PAY #### #### #### 9010 X'
    assert mask('4000-1234-5678-9010') == '####-####-####-9010'
    assert mask('acct 1234.5678.9012') == 'acct ####.####.9012'


def test_short_group_left_alone():
    assert mask('Store 12 34') == 'Store 12 34'
    assert mask('2 PACK 99.95') == '2 PACK 99.95'


def test_dates_with_slashes_not_grouped():
    assert mask('Date 10/22/29 SHOP') == 'Date 10/22/29 SHOP'


def test_whitespace_collapsed():
    assert mask('  A \t B\n  C ') == 'A B C'


def test_empty():
    assert mask('') == ''


def test_iso_dates_are_not_masked():
    assert mask('PAID 2029-03-01 REF 77') == 'PAID 2029-03-01 REF 77'
    assert mask('2029-03-01-123456') == '####-##-##-##3456'    # a date joined to a long number is not exempt
    assert mask('ACCT 1234-5678') == 'ACCT ####-5678'


def test_more_group_separators():
    assert mask('PAY 4000_1234_5678_9010') == 'PAY ####_####_####_9010'
    assert mask('PAY 4000/1234/5678/9010') == 'PAY ####/####/####/9010'
    assert mask('REF 4000,1234,5678,9010') == 'REF ####,####,####,9010'
    assert mask('ID 4000+1234+5678+9010') == 'ID ####+####+####+9010'


def test_slash_dates_stay_readable():
    assert mask('PURCHASE 3/1/29 STORE') == 'PURCHASE 3/1/29 STORE'
    assert mask('PURCHASE 03/01/2029 STORE 1234') == 'PURCHASE 03/01/2029 STORE 1234'
    assert mask('ON 03/01/2029 CARD 1234 5678 9012') == 'ON 03/01/2029 CARD #### #### 9012'
    assert mask('2029-03-01 and 12/31/2029') == '2029-03-01 and 12/31/2029'
    assert mask('a\x00b 123456') == 'a b ##3456'


def test_separators_with_spaces_around_them():
    assert mask('CARD 4000 - 1234 - 5678 - 9010') == 'CARD #### - #### - #### - 9010'
    assert mask('ACCT 4000 /1234/ 5678') == 'ACCT #### /####/ 5678'
    assert mask('2 - 3') == '2 - 3'


def test_any_short_punctuation_run_joins_groups():
    for sep in (';', ':', ' | ', '·', '--', ' / ', '_', ' . ', '#'):
        assert mask('A 4000%s1234%s5678%s9010' % (sep, sep, sep)) == 'A ####%s####%s####%s9010' % (sep, sep, sep)
    assert mask('A 4000 ---- 1234') == 'A 4000 ---- 1234'          # four separator characters do not join
    assert mask('4000 03/01/2029 1234') == '4000 03/01/2029 1234'   # a date in between keeps both short runs
    assert mask('LINE 4000\n1234 5678') == 'LINE #### #### 5678'     # whitespace is collapsed first


def test_spaced_separator_runs_join_groups():
    assert mask('CARD 4000 -- 1234 -- 5678 -- 9010') == 'CARD #### -- #### -- #### -- 9010'
    assert mask('CARD 4000 ;; 1234 | 5678') == 'CARD #### ;; #### | 5678'
    assert mask('ON 03/01/2029 -- 1234') == 'ON 03/01/2029 -- 1234'
