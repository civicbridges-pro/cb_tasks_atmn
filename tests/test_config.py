"""Config validation. These rules are the ledger invariants, so the tests are strict."""

from __future__ import annotations

import copy
import unittest

from cbops import config as config_mod
from tests.support import load_config


class ShippedConfigTest(unittest.TestCase):
    def test_no_errors(self):
        cfg = load_config()
        errors = [p for p in cfg.problems if p.level == "error"]
        self.assertEqual(errors, [], f"shipped config has errors: {errors}")

    def test_every_routed_owner_is_a_single_named_human(self):
        cfg = load_config()
        for rule in cfg.routing_rules:
            owner = cfg.owner_for_type(rule["type"])
            self.assertTrue(
                owner == config_mod.TRIAGE_QUEUE or cfg.is_person(owner),
                f"{rule['type']} owner {owner!r} is not a person",
            )

    def test_escalation_never_terminates_at_the_backup(self):
        """An escalation path that goes to the backup is not an escalation."""
        cfg = load_config()
        for rule in cfg.routing_rules:
            escalates = rule.get("escalates_to")
            escalates = escalates if isinstance(escalates, list) else [escalates]
            self.assertNotEqual(
                set(filter(None, escalates)), {rule.get("backup")},
                f"{rule['type']} escalates only to its own backup",
            )

    def test_escalation_path_ends_at_an_executive(self):
        cfg = load_config()
        exec_ids = set(cfg.group("exec"))
        for rule in cfg.routing_rules:
            path = cfg.escalation_path(rule["type"])
            self.assertTrue(exec_ids & set(path), f"{rule['type']} never reaches an exec")

    def test_phase_zero_is_read_only(self):
        cfg = load_config()
        self.assertEqual(cfg.phase, 0)
        self.assertFalse(cfg.outbound_enabled)

    def test_government_is_never_autosendable(self):
        """Guardrail 1. Checked against the rule, not through the phase gate."""
        cfg = load_config()
        never = set(cfg.guardrails["outbound"]["never_autosend_to_classes"])
        self.assertIn("gov", never)
        allowed, reason = cfg.may_autosend("vendor_chase", "gov")
        self.assertFalse(allowed, reason)

    def test_high_consequence_types_always_reach_a_human(self):
        cfg = load_config()
        for obligation_type in ("stop_work", "contract_action", "award"):
            self.assertTrue(
                cfg.needs_human_review(obligation_type, confidence=0.99),
                f"{obligation_type} bypassed human review at high confidence",
            )

    def test_stop_work_pages_despite_an_offset_hours_owner(self):
        """§6: stop-work must page immediately. §7 routes it to an offset-hours person."""
        cfg = load_config()
        rule = cfg.rule_for_type("stop_work")
        self.assertTrue(rule.get("page_immediately"))
        self.assertTrue(rule.get("ignore_coverage_hours"))
        primary = cfg.person(rule["primary"])
        if primary.get("offset_hours"):
            self.assertTrue(
                rule.get("also_notify"),
                "an offset-hours primary with no also_notify list means the page waits",
            )

    def test_sla_gov_is_tighter_than_sled(self):
        cfg = load_config()
        gov = cfg.sla_for("solicitation", "gov")
        sled = cfg.sla_for("solicitation", "customer_sled")
        self.assertLess(gov["response_hours"], sled["response_hours"])

    def test_deadline_driven_types_name_their_deadline_field(self):
        cfg = load_config()
        for name, spec in cfg.sla["types"].items():
            if spec.get("deadline_driven"):
                self.assertTrue(spec.get("deadline_field"), f"{name} has no deadline_field")


class ValidatorTest(unittest.TestCase):
    """The validator has to catch these, or a bad config ships silently."""

    def _mutated(self, section: str, mutate) -> list:
        cfg = load_config()
        clone = config_mod.Config(
            people=copy.deepcopy(cfg.people), routing=copy.deepcopy(cfg.routing),
            sla=copy.deepcopy(cfg.sla), guardrails=copy.deepcopy(cfg.guardrails),
            naming=copy.deepcopy(cfg.naming),
            counterparties=copy.deepcopy(cfg.counterparties),
        )
        mutate(getattr(clone, section))
        return config_mod.validate(clone)

    def _errors(self, problems, needle: str) -> bool:
        return any(p.level == "error" and needle in p.where for p in problems)

    def test_group_owner_is_rejected(self):
        def mutate(routing):
            routing["rules"][0]["primary"] = ["jason", "joe"]
        self.assertTrue(self._errors(self._mutated("routing", mutate), ".primary"))

    def test_unknown_person_is_rejected(self):
        def mutate(routing):
            routing["rules"][0]["primary"] = "nobody"
        self.assertTrue(self._errors(self._mutated("routing", mutate), ".primary"))

    def test_backup_equal_to_primary_is_rejected(self):
        def mutate(routing):
            routing["rules"][0]["backup"] = routing["rules"][0]["primary"]
        self.assertTrue(self._errors(self._mutated("routing", mutate), ".backup"))

    def test_outbound_in_phase_zero_is_rejected(self):
        def mutate(guardrails):
            guardrails["outbound"]["enabled"] = True
        self.assertTrue(
            self._errors(self._mutated("guardrails", mutate), "outbound.enabled")
        )

    def test_type_both_banned_and_autonomous_is_rejected(self):
        def mutate(guardrails):
            guardrails["outbound"]["autonomy_candidates"].append("stop_work")
        self.assertTrue(self._errors(self._mutated("guardrails", mutate), "guardrails.outbound"))

    def test_page_immediately_with_offset_owner_and_no_fanout_is_rejected(self):
        def mutate(routing):
            for rule in routing["rules"]:
                if rule["type"] == "stop_work":
                    rule.pop("also_notify", None)
        self.assertTrue(self._errors(self._mutated("routing", mutate), "routing.stop_work"))

    def test_broken_coverage_window_is_rejected(self):
        def mutate(people):
            people["people"]["jason"]["coverage_hours"] = "7am-5pm"
        self.assertTrue(self._errors(self._mutated("people", mutate), "coverage_hours"))

    def test_escalation_equal_to_backup_warns(self):
        def mutate(routing):
            routing["rules"][0]["escalates_to"] = routing["rules"][0]["backup"]
        problems = self._mutated("routing", mutate)
        self.assertTrue(any(p.level == "warn" and "escalates_to" in p.where for p in problems))


if __name__ == "__main__":
    unittest.main()
