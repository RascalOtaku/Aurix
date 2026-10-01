import unittest

from src.foundation.actions import run_action


class ActionsArgTest(unittest.TestCase):
    def test_gp_arm_invalid_arg(self):
        res = run_action("gp_arm", "abc")
        self.assertFalse(res["ok"])
        self.assertIn("number", res["message"])

    def test_teacher_on_invalid_arg(self):
        res = run_action("teacher_on", "xyz")
        self.assertFalse(res["ok"])
        self.assertIn("number", res["message"])
