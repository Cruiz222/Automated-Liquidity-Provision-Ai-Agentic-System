import unittest

from app.ledger import InvalidOperation, WalletLedger
from app.payouts import AmbiguousBank, BankDirectory, PayoutAccountNotFound, PayoutAccounts, SimulatedNameEnquiry, nuban


def account_for(code: str, serial: str) -> str:
    return serial + str(nuban.check_digit(code, serial))


class TestNubanAlgorithm(unittest.TestCase):
    def test_cbn_published_worked_examples(self):
        self.assertEqual(nuban.check_digit("011", "000001457"), 9)       # First Bank -> 0000014579
        self.assertEqual(nuban.check_digit("011", "000000022"), 0)       # remainder-zero branch -> 0000000220
        self.assertEqual(nuban.check_digit("50547", "000021457"), 9)     # other financial institution -> 0000214579
        self.assertTrue(nuban.validates("011", "0000014579"))
        self.assertTrue(nuban.validates("011", "0000000220"))
        self.assertTrue(nuban.validates("50547", "0000214579"))
        self.assertTrue(nuban.validates("000011", "0000014579"))          # 6-digit form of the same code

    def test_every_single_digit_typo_is_caught(self):
        good = account_for("058", "012345678")
        for position in range(10):
            for digit in "0123456789":
                if digit != good[position]:
                    typo = good[:position] + digit + good[position + 1:]
                    self.assertFalse(nuban.validates("058", typo), typo)

    def test_input_rules(self):
        for bad in ("12", "abcdefghij", "01234567890", "", None, 123456789):
            self.assertFalse(nuban.validates("058", bad))
        for code in ("", "12", "1234", "1234567", "abc", None):
            with self.assertRaises(ValueError):
                nuban.institution_seed(code)
        with self.assertRaises(ValueError):
            nuban.check_digit("058", "12345")


class TestDirectory(unittest.TestCase):
    def setUp(self):
        self.directory = BankDirectory.load()

    def test_starter_list_is_well_formed(self):
        codes = [b.code for b in self.directory.banks]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(c.isdigit() and len(c) == 3 for c in codes))
        self.assertEqual(self.directory.get("058").name, "Guaranty Trust Bank (GTBank)")
        # codes the published lists disagree on are deliberately not shipped
        self.assertIsNone(self.directory.get("039"))
        self.assertIsNone(self.directory.get("221"))

    def test_candidates_always_include_the_true_bank(self):
        for bank in self.directory.banks:
            account = account_for(bank.code, "123456789")
            self.assertIn(bank.code, [b.code for b in self.directory.candidates(account)])

    def test_check_digit_alone_is_usually_ambiguous(self):
        # why a name confirmation is required: many accounts validate for several banks
        counts = [len(self.directory.candidates(f"{n:09d}" + "0")) for n in range(1, 400)]
        self.assertGreater(sum(c > 1 for c in counts), 50)

    def test_bad_account_numbers(self):
        for bad in ("123", "abcdefghij", ""):
            with self.assertRaises(InvalidOperation):
                self.directory.candidates(bad)

    def test_duplicate_codes_rejected(self):
        with self.assertRaises(ValueError):
            BankDirectory([self.directory.banks[0], self.directory.banks[0]])


class ForcedNames:
    """Name enquiry that 'recognises' the account at every listed bank (worst case: ambiguous)."""

    def __init__(self, codes):
        self.codes = codes

    def lookup(self, bank_code, account_number):
        return f"NAME AT {bank_code}" if bank_code in self.codes else None


class PayoutCase(unittest.TestCase):
    def setUp(self):
        self.ledger = WalletLedger()
        self.addCleanup(self.ledger.close)
        self.directory = BankDirectory.load()
        self.payouts = PayoutAccounts(self.ledger, self.directory, SimulatedNameEnquiry(self.directory))


class TestResolution(PayoutCase):
    def test_with_the_full_directory_every_account_number_has_a_candidate(self):
        # Each bank's code shifts the check digit by a constant and the listed banks cover all ten residues, so the
        # check digit cannot reject a mistyped number when the bank is unknown. Only a name enquiry can.
        for n in range(1, 300):
            self.assertGreaterEqual(len(self.directory.candidates(f"{n:09d}0")), 1)

    def test_simulated_enquiry_names_exactly_one_candidate(self):
        for n in range(1, 200):
            number = f"{n:09d}"
            number += str(nuban.check_digit("033", number))
            named = [c for c in self.payouts.resolve(number) if c["account_name"]]
            self.assertEqual(len(named), 1)
            self.assertTrue(named[0]["account_name"].startswith("SIMULATED:"))

    def test_bank_code_can_be_omitted(self):
        number = account_for("058", "012345678")
        full = self.payouts.complete_ngn_destination({"account_number": number})
        self.assertEqual(full["account_number"], number)
        self.assertTrue(nuban.validates(full["bank_code"], number))
        self.assertTrue(full["account_name"])

    def test_given_bank_code_is_checked_against_the_account_number(self):
        number = account_for("058", "012345678")
        self.assertEqual(self.payouts.complete_ngn_destination({"account_number": number, "bank_code": "058"})["bank_name"],
                         "Guaranty Trust Bank (GTBank)")
        wrong_bank = next(b.code for b in self.directory.banks if not nuban.validates(b.code, number))
        with self.assertRaises(InvalidOperation):
            self.payouts.complete_ngn_destination({"account_number": number, "bank_code": wrong_bank})

    def test_unlisted_bank_code_is_accepted_but_unchecked(self):
        full = self.payouts.complete_ngn_destination({"account_number": "0123456785", "bank_code": "50515"})
        self.assertEqual((full["bank_code"], full["bank_name"], full["account_name"]), ("50515", "", ""))

    def test_no_candidate_bank_is_an_error_with_guidance(self):
        from app.payouts import Bank
        small = BankDirectory([Bank("058", "Guaranty Trust Bank (GTBank)")])
        payouts = PayoutAccounts(self.ledger, small, SimulatedNameEnquiry(small))
        number = account_for("058", "012345678")[:9] + "0"          # wrong check digit for the only listed bank
        self.assertEqual(small.candidates(number), [])
        with self.assertRaises(InvalidOperation) as ctx:
            payouts.complete_ngn_destination({"account_number": number})
        self.assertIn("bank code", str(ctx.exception))

    def test_ambiguity_is_surfaced_not_guessed(self):
        number = next(f"{n:09d}0" for n in range(1000) if len(self.directory.candidates(f"{n:09d}0")) >= 2)
        codes = [b.code for b in self.directory.candidates(number)]
        both = PayoutAccounts(self.ledger, self.directory, ForcedNames(set(codes)))
        with self.assertRaises(AmbiguousBank) as ctx:
            both.complete_ngn_destination({"account_number": number})
        self.assertEqual({c["bank_code"] for c in ctx.exception.candidates}, set(codes))
        none = PayoutAccounts(self.ledger, self.directory, ForcedNames(set()))
        with self.assertRaises(AmbiguousBank):                       # nobody recognised the name: the customer must choose
            none.complete_ngn_destination({"account_number": number})

    def test_malformed_destinations(self):
        for dest in ({}, {"account_number": "123"}, {"account_number": 1234567890}, {"account_number": "0123456785", "bank_code": "x1"}):
            with self.assertRaises(InvalidOperation):
                self.payouts.complete_ngn_destination(dest)


class TestSavedAccounts(PayoutCase):
    def test_add_list_delete_and_privacy(self):
        number = account_for("058", "012345678")
        saved = self.payouts.add("alice", "NGN", "My GTBank", {"account_number": number})
        self.assertTrue(saved["beneficiary_id"].startswith("b_"))
        self.assertEqual(saved["destination"], {"account_number": number, "bank_code": saved["destination"]["bank_code"]})
        self.assertEqual(len(self.payouts.list("alice")), 1)
        self.assertEqual(self.payouts.list("mallory"), [])
        with self.assertRaises(PayoutAccountNotFound):
            self.payouts.get("mallory", saved["beneficiary_id"])
        with self.assertRaises(PayoutAccountNotFound):
            self.payouts.delete("mallory", saved["beneficiary_id"])
        self.payouts.delete("alice", saved["beneficiary_id"])
        self.assertEqual(self.payouts.list("alice"), [])

    def test_duplicates_labels_and_limit(self):
        number = account_for("058", "012345678")
        self.payouts.add("alice", "NGN", "One", {"account_number": number})
        with self.assertRaises(InvalidOperation):
            self.payouts.add("alice", "NGN", "Again", {"account_number": number})
        for bad in ("", "x" * 41, "<script>alert(1)</script>", None, 5):
            with self.assertRaises(InvalidOperation):
                self.payouts.add("alice", "NGN", bad, {"account_number": account_for("033", "000000011")})

    def test_per_user_limit(self):
        from app.payouts import MAX_BENEFICIARIES
        for i in range(MAX_BENEFICIARIES):
            self.payouts.add("alice", "USDT", f"wallet {i}", {"network": "ERC20", "address": "0x" + f"{i:040x}"})
        with self.assertRaises(InvalidOperation):
            self.payouts.add("alice", "USDT", "one too many", {"network": "ERC20", "address": "0x" + "f" * 40})

    def test_usdt_destinations_use_ledger_validation(self):
        self.payouts.add("alice", "USDT", "tron", {"network": "TRC20", "address": "T" + "A" * 33})
        with self.assertRaises(InvalidOperation):
            self.payouts.add("alice", "USDT", "bad", {"network": "TRC20", "address": "0x" + "a" * 40})


if __name__ == "__main__":
    unittest.main()
