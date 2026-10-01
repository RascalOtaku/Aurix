import unittest

from src.foundation.growth import _age


class T(unittest.TestCase):
    def test_none_input(self):
        self.assertEqual(_age(None), 'never')
