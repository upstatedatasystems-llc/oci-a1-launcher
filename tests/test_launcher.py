"""Regression unit tests for OCI A1 Launcher v1.2.1 hermetic test isolation and stack selection logic."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src/ directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import common
import launcher
from common import (
    StackSpec,
    classify_a1_instances,
    load_stacks,
    parse_extra_small_stacks,
)
from launcher import (
    choose_small_stack,
    get_candidate_plan,
    pair_for_rotation,
    validate_inventory,
)


class TestLauncherV121(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp_dir.name)

        os.environ["DATA_DIR"] = str(temp_path)
        os.environ["HOME"] = str(temp_path)

        self._patchers = [
            patch("common.DATA_DIR", temp_path),
            patch("common.STATE_FILE", temp_path / "state.json"),
            patch("common.EVENT_FILE", temp_path / "events.jsonl"),
            patch("common.LOCK_FILE", temp_path / "launcher.lock"),
            patch("common.PAUSE_FILE", temp_path / "PAUSED"),
            patch("common.COMPLETE_FILE", temp_path / "COMPLETE.json"),
            patch("common.THROTTLE_FILE", temp_path / "THROTTLED.json"),
            patch("launcher.PAUSE_FILE", temp_path / "PAUSED"),
            patch("launcher.COMPLETE_FILE", temp_path / "COMPLETE.json"),
            patch("launcher.THROTTLE_FILE", temp_path / "THROTTLED.json"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patchers):
            p.stop()
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_case_a_and_b_c_existing_ad3_success_and_extra_stack(self) -> None:
        """Test Cases A, B, C: Existing AD3 success excludes old stack, but allows new stack in AD-3."""
        os.environ["STACK_OCID_AD1"] = "ocid1.ormstack.ad1_large"
        os.environ["STACK_OCID_AD1E"] = "ocid1.ormstack.ad1_small"
        os.environ["STACK_OCID_AD2"] = "ocid1.ormstack.ad2_large"
        os.environ["STACK_OCID_AD2E"] = "ocid1.ormstack.ad2_small"
        os.environ["STACK_OCID_AD3"] = "ocid1.ormstack.ad3_large"
        os.environ["STACK_OCID_AD3E"] = "ocid1.ormstack.ad3_small_old"
        os.environ["EXTRA_SMALL_STACKS_JSON"] = (
            '[{"name": "purgatory03-ad3e", "ocid": "ocid1.ormstack.ad3_small_new", "ad": 3}]'
        )

        loaded = load_stacks()
        with patch("common.STACKS", loaded), patch("launcher.STACKS", loaded):
            self.assertEqual(len(loaded), 7)

            state = {
                "next_ad_index": 2,  # Points to AD-3
                "successful_stack_ocids": ["ocid1.ormstack.ad3_small_old"],
                "successful_instance_ids": ["ocid1.instance.ad3_vm1"],
            }
            existing_small = [
                {
                    "id": "ocid1.instance.ad3_vm1",
                    "display_name": "a1-ad3-vm",
                    "ocpus": 1.0,
                    "memory_gb": 6.0,
                    "availability_domain": "US-ASHBURN-AD-3",
                }
            ]

            selected = choose_small_stack(state, existing_small, preferred_ad=3)
            self.assertEqual(selected.name, "purgatory03-ad3e")
            self.assertEqual(selected.ocid, "ocid1.ormstack.ad3_small_new")
            self.assertNotEqual(selected.ocid, "ocid1.ormstack.ad3_small_old")

    def test_case_d_duplicate_extra_stacks_rejected(self) -> None:
        """Test Case D: Duplicate stack names or OCIDs are rejected."""
        os.environ["STACK_OCID_AD1"] = "ocid1.ormstack.ad1"
        os.environ["STACK_OCID_AD1E"] = "ocid1.ormstack.ad1e"
        os.environ["STACK_OCID_AD2"] = "ocid1.ormstack.ad2"
        os.environ["STACK_OCID_AD2E"] = "ocid1.ormstack.ad2e"
        os.environ["STACK_OCID_AD3"] = "ocid1.ormstack.ad3"
        os.environ["STACK_OCID_AD3E"] = "ocid1.ormstack.ad3e"

        # Duplicate stack name
        os.environ["EXTRA_SMALL_STACKS_JSON"] = (
            '[{"name": "purgatory02-ad1e", "ocid": "ocid1.ormstack.new_unique", "ad": 1}]'
        )
        with self.assertRaises(ValueError) as ctx:
            load_stacks()
        self.assertIn("Duplicate stack name", str(ctx.exception))

        # Duplicate stack OCID
        os.environ["EXTRA_SMALL_STACKS_JSON"] = (
            '[{"name": "purgatory03-ad1e", "ocid": "ocid1.ormstack.ad1e", "ad": 1}]'
        )
        with self.assertRaises(ValueError) as ctx:
            load_stacks()
        self.assertIn("Duplicate stack OCID", str(ctx.exception))

    def test_case_e_invalid_ad_values_rejected(self) -> None:
        """Test Case E: Invalid AD values (e.g. 4, 0, or non-int) are rejected."""
        invalid_jsons = [
            '[{"name": "purgatory03-ad4", "ocid": "ocid1.ormstack.ad4", "ad": 4}]',
            '[{"name": "purgatory03-ad0", "ocid": "ocid1.ormstack.ad0", "ad": 0}]',
            '[{"name": "purgatory03-adX", "ocid": "ocid1.ormstack.adX", "ad": "invalid"}]',
        ]
        for ij in invalid_jsons:
            with self.subTest(json_val=ij):
                with self.assertRaises(ValueError):
                    parse_extra_small_stacks(ij)

    def test_case_f_existing_2_12_behavior_unchanged(self) -> None:
        """Test Case F: Existing 2/12 candidate-selection behavior remains unchanged."""
        os.environ["STACK_OCID_AD1"] = "ocid1.ormstack.ad1_2_12"
        os.environ["STACK_OCID_AD1E"] = "ocid1.ormstack.ad1_1_6"
        os.environ["STACK_OCID_AD2"] = "ocid1.ormstack.ad2_2_12"
        os.environ["STACK_OCID_AD2E"] = "ocid1.ormstack.ad2_1_6"
        os.environ["STACK_OCID_AD3"] = "ocid1.ormstack.ad3_2_12"
        os.environ["STACK_OCID_AD3E"] = "ocid1.ormstack.ad3_1_6"

        with patch("common.STACKS", load_stacks()):
            large, small = pair_for_rotation({"next_ad_index": 0})
            self.assertEqual(large.name, "purgatory02-ad1")
            self.assertEqual(large.ocpus, 2.0)
            self.assertEqual(large.memory_gb, 12.0)
            self.assertEqual(small.name, "purgatory02-ad1e")

            large2, small2 = pair_for_rotation({"next_ad_index": 1})
            self.assertEqual(large2.name, "purgatory02-ad2")

    def test_case_g_quota_and_completion_safeguards(self) -> None:
        """Test Case G: Existing quota/completion safeguards remain effective."""
        instances_large = [{"ocpus": 2.0, "memory_gb": 12.0}]
        res1 = classify_a1_instances(instances_large)
        self.assertTrue(res1["complete"])
        self.assertIsNone(validate_inventory(res1))

        instances_two_small = [
            {"ocpus": 1.0, "memory_gb": 6.0},
            {"ocpus": 1.0, "memory_gb": 6.0},
        ]
        res2 = classify_a1_instances(instances_two_small)
        self.assertTrue(res2["complete"])
        self.assertIsNone(validate_inventory(res2))

        instances_mixed = [
            {"ocpus": 2.0, "memory_gb": 12.0},
            {"ocpus": 1.0, "memory_gb": 6.0},
        ]
        res3 = classify_a1_instances(instances_mixed)
        self.assertIsNotNone(validate_inventory(res3))

    @patch("launcher.create_oci_clients")
    def test_case_h_candidates_plan_is_read_only(self, mock_create_clients: MagicMock) -> None:
        """Test Case H: Candidates/plan diagnostic command is strictly read-only and cannot submit APPLY jobs."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["STACK_OCID_AD1"] = "ocid1.ormstack.ad1"
        os.environ["STACK_OCID_AD1E"] = "ocid1.ormstack.ad1e"
        os.environ["STACK_OCID_AD2"] = "ocid1.ormstack.ad2"
        os.environ["STACK_OCID_AD2E"] = "ocid1.ormstack.ad2e"
        os.environ["STACK_OCID_AD3"] = "ocid1.ormstack.ad3"
        os.environ["STACK_OCID_AD3E"] = "ocid1.ormstack.ad3e"

        with patch("common.STACKS", load_stacks()):
            plan = get_candidate_plan()

            self.assertIn("paused", plan)
            self.assertIn("all_stacks", plan)
            self.assertIn("next_large_candidate", plan)
            self.assertIn("next_small_candidate", plan)

            mock_rm.create_job.assert_not_called()
            mock_rm.change_stack_compartment.assert_not_called()

    @patch("launcher.create_oci_clients")
    def test_hermetic_isolation_with_inaccessible_production_data_dir(
        self, mock_create_clients: MagicMock
    ) -> None:
        """Regression Test: Suite operates cleanly without permission errors even if production /var/lib/oci-a1-launcher is inaccessible."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["STACK_OCID_AD1"] = "ocid1.ormstack.ad1"
        os.environ["STACK_OCID_AD1E"] = "ocid1.ormstack.ad1e"
        os.environ["STACK_OCID_AD2"] = "ocid1.ormstack.ad2"
        os.environ["STACK_OCID_AD2E"] = "ocid1.ormstack.ad2e"
        os.environ["STACK_OCID_AD3"] = "ocid1.ormstack.ad3"
        os.environ["STACK_OCID_AD3E"] = "ocid1.ormstack.ad3e"

        # Verify that get_candidate_plan reads from the isolated temp_dir patched in setUp,
        # without raising PermissionError or touching root-only production paths.
        with patch("common.STACKS", load_stacks()):
            plan = get_candidate_plan()
            self.assertIn("paused", plan)
            self.assertFalse(plan["paused"])
            self.assertIn("all_stacks", plan)


if __name__ == "__main__":
    unittest.main()
