"""Tests for src/foundation/capabilities.py - driven by the owner's real example goals."""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import capabilities as cp  # noqa: E402

NOTHING = dict(env={}, which=lambda b: None, find_spec=lambda m: None, authorizations={}, probe=lambda h, p: False)

GOALS = {
    "ct": "Convert CT scan of skull into clean 3d print file and find cheapest option to produce",
    "reynolds": "Reynolds Gang ongoing treasure hunt",
    "trading": "Daily stock trading",
    "bounty": "Bug Bounties",
    "media": "Transcoding and transcription services",
    "improve": "Self improve ongoing",
    "social": "Generate content for profit on socials",
    "homes": "Finding homes for sale and taking cut off",
}


class RegistryTests(unittest.TestCase):
    def test_every_capability_named_by_a_pack_exists(self):
        for pack in cp.PACKS:
            names = list(pack.capabilities) + [c for s in pack.steps for c in s.capabilities]
            for n in names:
                self.assertIsNotNone(cp.lookup(n), f"pack {pack.id} names unknown capability {n!r}")

    def test_pack_regexes_compile(self):
        for pack in cp.PACKS:
            for pat in pack.triggers + pack.prohibited_patterns:
                re.compile(pat)

    def test_lookup_by_id_alias_and_case(self):
        self.assertEqual(cp.lookup("dicom").id, "pydicom")
        self.assertEqual(cp.lookup("Whisper").id, "faster-whisper")
        self.assertEqual(cp.lookup("sitk").id, "SimpleITK")
        self.assertEqual(cp.lookup("SIMPLEITK").id, "SimpleITK")
        self.assertEqual(cp.lookup("scikit_image").id, "scikit-image")
        self.assertIsNone(cp.lookup("teleporter"))
        self.assertIsNone(cp.lookup(""))

    def test_only_pip_installs_are_autonomous(self):
        for cap in cp.REGISTRY.values():
            if cap.install:
                self.assertEqual(cap.install.autonomous, cap.install.where == "container_pip")
        self.assertFalse(cp.REGISTRY["ffmpeg"].install.autonomous)      # needs Dockerfile / winget
        self.assertFalse(cp.REGISTRY["nuclei"].install.autonomous)      # security tools: owner installs

    def test_pip_commands_are_medium_risk_under_the_risk_model(self):
        from src.foundation import risk
        for cap in cp.REGISTRY.values():
            if cap.install and cap.install.autonomous:
                self.assertEqual(risk.assess_bash(cap.install.command).tier, risk.RiskTier.MEDIUM, cap.id)


class PresenceTests(unittest.TestCase):
    def test_python_and_binary(self):
        pyd, ff = cp.REGISTRY["pydicom"], cp.REGISTRY["ffmpeg"]
        self.assertTrue(cp.presence(pyd, find_spec=lambda m: object()).present)
        self.assertFalse(cp.presence(pyd, find_spec=lambda m: None).present)
        self.assertTrue(cp.presence(ff, which=lambda b: "/usr/bin/ffmpeg").present)
        self.assertFalse(cp.presence(ff, which=lambda b: None).present)

    def test_credential_never_leaks_the_value(self):
        cap = cp.REGISTRY["alpaca-credentials"]
        p = cp.presence(cap, env={"ALPACA_KEY": "SUPERSECRET"})
        self.assertTrue(p.present)
        self.assertNotIn("SUPERSECRET", p.detail)
        self.assertFalse(cp.presence(cap, env={}).present)

    def test_authorizations_and_expiry(self):
        cap = cp.REGISTRY["bugbounty-scope"]
        self.assertFalse(cp.presence(cap, authorizations={}).present)
        self.assertTrue(cp.presence(cap, authorizations={"bugbounty-scope": {"granted_by": "rascal"}}).present)
        self.assertFalse(cp.presence(cap, authorizations={"bugbounty-scope": {"expires": "2000-01-01"}}).present)
        self.assertTrue(cp.presence(cap, authorizations={"bugbounty-scope": {"expires": "2999-01-01"}}).present)
        self.assertFalse(cp.presence(cap, authorizations={"bugbounty-scope": "yes"}).present)

    def test_authorizations_file(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": tmp}):
            self.assertEqual(cp.load_authorizations(), {})
            p = cp.authorizations_path()
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"live-trading": {"granted_by": "rascal"}}))
            self.assertTrue(cp.presence(cp.REGISTRY["live-trading"]).present)
            p.write_text("not json")
            self.assertEqual(cp.load_authorizations(), {})

    def test_service_needs_a_probe(self):
        cap = cp.REGISTRY["gpu-box"]
        self.assertFalse(cp.presence(cap).present)                       # never touches the network by itself
        self.assertTrue(cp.presence(cap, probe=lambda h, p: (h, p) == ("100.64.0.10", 11434)).present)


class IdentifyTests(unittest.TestCase):
    def ident(self, key, **kw):
        return cp.identify(GOALS[key], **{**NOTHING, **kw})

    def test_each_real_goal_matches_exactly_its_pack(self):
        expected = {"ct": "ct_to_print", "reynolds": "reynolds_research", "trading": "trading_paper",
                    "bounty": "bug_bounty", "media": "transcription", "improve": "self_improve",
                    "social": "social_content", "homes": "real_estate_leads"}
        for key, pack in expected.items():
            req, packs = self.ident(key)
            self.assertEqual([p.id for p in packs], [pack], f"{key}: {[p.id for p in packs]}")

    def test_ct_scan_requirements(self):
        req, _ = self.ident("ct")
        for pkg in ("pydicom", "SimpleITK", "scikit-image", "trimesh", "pymeshlab", "numpy-stl"):
            self.assertIn(pkg, req.installable)                           # AURIX can pip these itself
        self.assertTrue(any("patient-data-consent" in m for m in req.manual))
        self.assertTrue(any("NOT clinical" in n for n in req.legal))
        self.assertIn("web_search", req.present)                          # built-in tool
        self.assertEqual(req.unknown, [])

    def test_real_estate_flags_the_license(self):
        req, _ = self.ident("homes")
        self.assertTrue(any("real-estate-license" in m for m in req.manual))
        self.assertTrue(any("Colorado" in n and "license" in n for n in req.legal))

    def test_trading_is_paper_only_and_live_needs_owner(self):
        req, packs = self.ident("trading")
        self.assertTrue(any("live-trading" in m for m in req.manual))
        self.assertTrue(any("PAPER" in n for n in req.legal))
        pats = packs[0].prohibited_patterns
        live = "curl https://api.alpaca.markets/v2/orders"
        paper = "curl https://paper-api.alpaca.markets/v2/orders"
        self.assertTrue(any(re.search(p, live) for p in pats))
        self.assertFalse(any(re.search(p, paper) for p in pats))
        self.assertTrue(any(re.search(p, "export ALPACA_LIVE=true") for p in pats))

    def test_bug_bounty_needs_written_scope_and_owner_installs_scanners(self):
        req, _ = self.ident("bounty")
        self.assertTrue(any("bugbounty-scope" in m for m in req.manual))
        self.assertTrue(any(o.startswith("nuclei:") for o in req.owner_install))
        self.assertTrue(any("in scope" in n.lower() for n in req.legal))

    def test_transcription_ffmpeg_is_an_owner_install_not_a_silent_apt(self):
        req, _ = self.ident("media")
        self.assertTrue(any(o.startswith("ffmpeg:") for o in req.owner_install))
        self.assertIn("faster-whisper", req.installable)
        self.assertNotIn("ffmpeg", req.installable)

    def test_legal_notes_survive_when_everything_is_present(self):
        everything = dict(env={k: "x" for k in ("ALPACA_KEY", "ANTHROPIC_API_KEY")},
                          which=lambda b: "/bin/x", find_spec=lambda m: object(),
                          authorizations={k: {"granted_by": "rascal"} for k in
                                          ("real-estate-license", "live-trading", "bugbounty-scope",
                                           "patient-data-consent", "social-accounts", "content-rights")},
                          probe=lambda h, p: True)
        req, _ = cp.identify(GOALS["homes"], **everything)
        self.assertTrue(req.ready)
        self.assertTrue(any("Colorado" in n for n in req.legal))          # pack legal notes never dropped

    def test_unmatched_goal_yields_nothing_and_extra_unknowns_are_flagged(self):
        req, packs = cp.identify("bake a cake", extra_capabilities=["oven-control"], **NOTHING)
        self.assertEqual(packs, [])
        self.assertEqual(req.unknown, ["oven-control"])
        self.assertEqual(req.legal, [])
        self.assertFalse(req.ready)

    def test_multi_domain_goal(self):
        req, packs = cp.identify("transcribe interviews and post shorts on youtube", **NOTHING)
        self.assertEqual({p.id for p in packs}, {"transcription", "social_content"})

    def test_as_dict_feeds_the_proposal_renderer(self):
        req, _ = self.ident("ct")
        d = req.as_dict()
        for key in ("installable", "manual", "unknown", "legal", "owner_install"):
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main()
