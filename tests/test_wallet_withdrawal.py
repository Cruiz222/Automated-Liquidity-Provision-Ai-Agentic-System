import tempfile, unittest
from pathlib import Path
from app.wallet_ledger import IdempotencyConflict, InsufficientFunds, WalletLedger
class TestSimulatedWithdrawals(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.ledger=WalletLedger(Path(self.tmp.name)/"ledger.sqlite3"); self.ledger.deposit("alice","NGN",50000,"fund-1")
 def tearDown(self): self.ledger.close(); self.tmp.cleanup()
 def test_withdraw_is_idempotent(self):
  a=self.ledger.withdraw("alice","NGN",20000,"withdraw-1"); b=self.ledger.withdraw("alice","NGN",20000,"withdraw-1"); self.assertEqual(a,b); self.assertEqual(self.ledger.balance("alice","NGN"),30000)
 def test_insufficient_funds(self):
  with self.assertRaises(InsufficientFunds): self.ledger.withdraw("alice","NGN",60000,"withdraw-2")
  self.assertEqual(self.ledger.balance("alice","NGN"),50000)
 def test_conflicting_key(self):
  self.ledger.withdraw("alice","NGN",10000,"withdraw-3")
  with self.assertRaises(IdempotencyConflict): self.ledger.withdraw("alice","NGN",20000,"withdraw-3")
if __name__=="__main__": unittest.main()
