"""Tap instead of type: the button definitions, and the rule that a button carries no authority of its own."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import buttons, commands  # noqa: E402


def flat(kb):
    return [(b["text"], b["callback_data"]) for row in (kb or {}).get("inline_keyboard", []) for b in row]


class ForReplyTests(unittest.TestCase):
    def test_mission_proposal_gets_approve_and_deny(self):
        kb = buttons.for_reply("new", "Mission proposal [m-abc123] ... Reply <code>approve mission m-abc123</code> or <code>deny mission m-abc123</code>.")
        self.assertEqual([c for _, c in flat(kb)], ["do:approve mission m-abc123", "do:deny mission m-abc123"])

    def test_game_fix_gets_yes_no_and_no_undo_next_to_them(self):
        text = "🎮 fix <code>g-abc123</code> ... <code>yes g-abc123</code> or <code>no g-abc123</code> <i>(undo later with <code>undo g-abc123</code>)</i>"
        cmds = [c for _, c in flat(buttons.for_reply("", text))]
        self.assertEqual(cmds, ["do:yes g-abc123", "do:no g-abc123"])

    def test_several_fixes_are_told_apart_and_capped(self):
        text = " ".join(f"yes g-00000{i} no g-00000{i}" for i in range(1, 6))
        labels = [t for t, _ in flat(buttons.for_reply("fixes", text))]
        self.assertEqual(len(labels), 6)                                    # three fixes x (yes, no)
        self.assertTrue(all("00000" in lab for lab in labels))

    def test_applied_fix_offers_undo(self):
        kb = buttons.for_reply("", "✅ Fixed g-abc123 ... To take it back: undo g-abc123")
        self.assertEqual(flat(kb), [("↩️ Undo this fix", "do:undo g-abc123")])

    def test_lesson_buttons(self):
        self.assertEqual([c for _, c in flat(buttons.for_reply("lesson_show", "approve lesson l-abc123 or deny lesson l-abc123"))],
                         ["do:approve lesson l-abc123", "do:deny lesson l-abc123"])
        listing = "<b>Lessons</b> (1)\n📝 <code>l-abcdef</code> Validate types first · pending"
        self.assertIn("do:approve lesson l-abcdef", [c for _, c in flat(buttons.for_reply("lessons", listing))])

    def test_menus_on_overview_screens_and_help_placeholders_do_not_make_buttons(self):
        self.assertEqual(len(flat(buttons.for_reply("menu", "hi"))), 24)
        help_kb = flat(buttons.for_reply("help", commands.HELP))
        self.assertEqual(len(help_kb), 24)                                  # only the menu: "approve mission <id>" placeholders are not ids
        self.assertIn(("▶ Check everything", "do:all"), flat(buttons.for_reply("dashboard", "x")))
        self.assertIn(("🔄 Refresh facts", "do:games refresh"), flat(buttons.for_reply("games", "x")))

    def test_fast_lane_toggle_matches_the_state(self):
        self.assertEqual(flat(buttons.for_reply("fastlane", "⚡ Fast lane is ON")), [("⚡ Turn off", "do:fast lane off")])
        self.assertEqual(flat(buttons.for_reply("fastlane", "⚡ Fast lane is OFF")), [("⚡ Turn on", "do:fast lane on")])

    def test_nothing_to_offer_means_no_keyboard(self):
        self.assertIsNone(buttons.for_reply("ping", "pong"))
        self.assertIsNone(buttons.for_reply("", ""))

    def test_callback_data_fits_telegrams_limit(self):
        for kind in ("menu", "dashboard", "games", "fixes"):
            for _, data in flat(buttons.for_reply(kind, "x yes g-abc123 no g-abc123")):
                self.assertLessEqual(len(data.encode()), 64)


class AuthorityTests(unittest.TestCase):
    def test_data_must_be_ours_and_plain(self):
        self.assertEqual(buttons.command_from_data("do:yes g-abc123"), "yes g-abc123")
        for bad in ("approve:abc123", "", "do:", "do:x", "do:" + "a" * 70, "do:yes g-abc123; rm -rf /", "do:<script>", None):
            self.assertIsNone(buttons.command_from_data(bad), bad)

    def test_every_menu_and_generated_button_maps_to_an_allowed_kind(self):
        samples = [buttons.menu()]
        samples += [buttons.for_reply(k, "approve mission m-abc123 yes g-abc123 undo g-def456 approve lesson l-abc123 approve skill my_tool")
                    for k in ("new", "fixes", "dashboard", "games", "evals", "timeline")]
        for kb in samples:
            for _, data in flat(kb):
                parsed = commands.parse(buttons.command_from_data(data))
                self.assertIsNotNone(parsed, data)
                self.assertIn(parsed[0], buttons.ALLOWED_KINDS, data)

    def test_dangerous_commands_are_not_reachable_from_a_button(self):
        for cmd in ("stop", "authorize live-trading", "forge: rm everything", "mission: buy stuff", "project: x", "run skill x {}", "resume"):
            parsed = commands.parse(cmd)
            self.assertTrue(parsed is None or parsed[0] not in buttons.ALLOWED_KINDS, cmd)


if __name__ == "__main__":
    unittest.main()
