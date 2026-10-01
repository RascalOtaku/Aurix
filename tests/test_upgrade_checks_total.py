import unittest

from src.foundation.panels import checks


class T(unittest.TestCase):
    def test_checks_total_field(self):
        res = checks()
        if 'have' in res and res['have']:
            for tier, data in res['tiers'].items():
                self.assertIn('total', data, f"Tier {tier} is missing 'total' field")
