import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class BacktestCLITests(unittest.TestCase):
    def test_report_provenance_repeatability_and_output_protection(self):
        sample = Path(__file__).resolve().parents[1] / 'examples' / 'market_sample.csv'
        command = [sys.executable, '-m', 'app.backtest', str(sample)]
        first = subprocess.run(command, capture_output=True, text=True, check=True)
        second = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual(report['source']['sha256'], hashlib.sha256(sample.read_bytes()).hexdigest())
        self.assertEqual(report['market']['event_count'], 12)
        self.assertGreater(report['agent']['metrics']['fill_count'], 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            subprocess.run(command + ['--output', str(output)], capture_output=True, text=True, check=True)
            original = output.read_bytes()
            failed = subprocess.run(command + ['--output', str(output)], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn('Backtest failed', failed.stderr)
            self.assertEqual(output.read_bytes(), original)
        failed = subprocess.run(command + ['--participation', 'nan'], capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('participation', failed.stderr)
