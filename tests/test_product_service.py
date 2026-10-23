import tempfile, unittest
from pathlib import Path
from app.product_service import ProductService, NotFound
from app.wallet_ledger import InsufficientFunds, IdempotencyConflict, InvalidOperation

class ServiceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.s=ProductService(Path(self.tmp.name)/'db.sqlite3')
 def tearDown(self): self.s.close(); self.tmp.cleanup()
 def test_simulated_deposit_and_withdraw(self):
  self.s.deposit_simulated('alice','NGN',10000,'dep1'); self.s.withdraw_simulated('alice','NGN',3000,'wd1')
  self.assertEqual(self.s.balances('alice')['NGN'],7000)
 def test_withdraw_rejects_insufficient(self):
  with self.assertRaises(InsufficientFunds): self.s.withdraw_simulated('alice','USDT',10,'wd')
 def test_positions_are_scoped_and_close_is_idempotent(self):
  p=self.s.create_position('alice',dict(pool_id='p1',chain='base',dex='dex',token0='USDC',token1='USDT',deposited_usd='100'))
  with self.assertRaises(NotFound): self.s.get_position('bob',p['position_id'])
  self.assertEqual(self.s.close_position('alice',p['position_id'])['status'],'closed')
  self.assertEqual(self.s.close_position('alice',p['position_id'])['status'],'closed')
 def test_invalid_position_rejected(self):
  with self.assertRaises(InvalidOperation): self.s.create_position('alice',dict(pool_id='p',chain='base',dex='d',token0='USDC',token1='USDC',deposited_usd='1'))
 def test_transaction_limit_bounded(self):
  with self.assertRaises(InvalidOperation): self.s.transactions('alice',500)
if __name__=='__main__': unittest.main()
