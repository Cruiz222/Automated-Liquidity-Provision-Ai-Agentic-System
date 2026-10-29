from pathlib import Path
import tempfile
import unittest
from app.market_data import COLUMNS, MarketEvent, load_csv


class MarketDataTests(unittest.TestCase):
    def load(self, rows, header=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'market.csv'
            path.write_text((header or ','.join(COLUMNS)) + '\n' + '\n'.join(rows) + '\n')
            return load_csv(path)

    def test_valid_input_preserves_timestamp_ties(self):
        events = self.load(['0,99,101,1,2,,,', '0,99,101,1,2,99,0.5,sell'])
        self.assertEqual(events[0].mid, 100)
        self.assertEqual(events[1].trade_side, 'sell')

    def test_invalid_rows_identify_line(self):
        invalid = ['-1,99,101,1,2,,,', '1.5,99,101,1,2,,,',
                   '1,101,99,1,2,,,', '1,nan,101,1,2,,,',
                   '1,99,inf,1,2,,,', '1,99,101,-1,2,,,',
                   '1,99,101,1,2,99,,sell', '1,99,101,1,2,99,0,sell',
                   '1,99,101,1,2,99,1,bid', '1,99,101,1,2',
                   '1,99,101,1,2,,,,extra']
        for row in invalid:
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, 'CSV line 3'):
                self.load(['0,99,101,1,2,,,', row])

    def test_unsorted_header_and_short_input_rejected(self):
        with self.assertRaisesRegex(ValueError, 'nondecreasing'):
            self.load(['2,99,101,1,2,,,', '1,99,101,1,2,,,'])
        with self.assertRaisesRegex(ValueError, 'header'):
            self.load([], header='timestamp,price')
        with self.assertRaisesRegex(ValueError, 'At least two'):
            self.load(['0,99,101,1,2,,,'])

    def test_direct_event_validation(self):
        with self.assertRaises(ValueError):
            MarketEvent(0, 100, 100, 1, 1)
