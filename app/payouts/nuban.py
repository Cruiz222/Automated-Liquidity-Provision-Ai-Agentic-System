"""Nigerian NUBAN check-digit algorithm (CBN "Revised Standards on NUBAN", 2020).

A NUBAN is a 9-digit serial plus one check digit. The check digit is computed over a 6-digit institution code and
the serial, with weights 3, 7, 3 repeating across the 15 digits:

* a 3-digit bank code is padded with three leading zeros (011 -> 000011);
* a 5-digit other-financial-institution code is prefixed with 9 (50547 -> 950547);
* a 6-digit code is used as is.

check = (10 - (weighted sum mod 10)) mod 10.  Verified against the CBN's worked examples in the tests.

The check digit only catches typos and narrows an account number to a few possible banks (about one in ten
unrelated banks also validates). It cannot prove which bank holds the account; only a name enquiry can.
"""
from __future__ import annotations

WEIGHTS = (3, 7, 3)


def institution_seed(code: str) -> str:
    if not isinstance(code, str) or not code.isdigit():
        raise ValueError("institution code must be digits")
    if len(code) == 3:
        return "000" + code
    if len(code) == 5:
        return "9" + code
    if len(code) == 6:
        return code
    raise ValueError("institution code must have 3, 5 or 6 digits")


def check_digit(code: str, serial: str) -> int:
    if not isinstance(serial, str) or len(serial) != 9 or not serial.isdigit():
        raise ValueError("serial must be 9 digits")
    seed = institution_seed(code) + serial
    total = sum(int(digit) * WEIGHTS[i % 3] for i, digit in enumerate(seed))
    return (10 - total % 10) % 10


def is_valid_account_number(account_number: str) -> bool:
    return isinstance(account_number, str) and len(account_number) == 10 and account_number.isdigit()


def validates(code: str, account_number: str) -> bool:
    """True if ``account_number`` passes the NUBAN check for the institution ``code``."""
    if not is_valid_account_number(account_number):
        return False
    return check_digit(code, account_number[:9]) == int(account_number[9])
