"""Regression unit tests for OCI A1 Launcher v1.3.0 CLI dispatch, stack selection, and RESIZE_ONLY mode."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
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


class TestLauncherV130(unittest.TestCase):
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

        with patch("common.STACKS", load_stacks()):
            plan = get_candidate_plan()
            self.assertIn("paused", plan)
            self.assertFalse(plan["paused"])
            self.assertIn("all_stacks", plan)

    def test_status_command_execution_and_reporting(self) -> None:
        """Test status command execution with synthetic Paused, Complete, and Throttled states."""
        # 1. Base status output
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            ret = launcher.print_status()
            self.assertEqual(ret, 0)
            out = fake_out.getvalue()
            self.assertIn("Provisioning Mode: STANDARD", out)
            self.assertIn('"next_ad_index": 0', out)
            self.assertIn("Paused: False", out)
            self.assertIn("Complete: False", out)
            self.assertIn("Throttled: False", out)

        # 2. Synthetic Paused + Complete + Throttled state
        temp_path = Path(self.temp_dir.name)
        (temp_path / "PAUSED").touch()
        (temp_path / "COMPLETE.json").write_text('{"reason": "TEST_COMPLETE"}')
        cooldown_until = launcher.now_utc() + timedelta(seconds=1800)
        throttle_payload = {
            "until": launcher.utc_iso(cooldown_until),
            "status": 429,
            "code": "TooManyRequests",
        }
        (temp_path / "THROTTLED.json").write_text(json.dumps(throttle_payload))

        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            ret = launcher.print_status()
            self.assertEqual(ret, 0)
            out = fake_out.getvalue()
            self.assertIn("Paused: True", out)
            self.assertIn("Complete: True", out)
            self.assertIn("Throttled: True", out)
            self.assertIn("TEST_COMPLETE", out)

    def test_all_cli_dispatch_targets_exist_and_resolve(self) -> None:
        """Audit test: Verify every CLI command dispatched by main() resolves to an existing function."""
        with patch("sys.argv", ["launcher.py", "status"]), patch("launcher.print_status", return_value=0) as mock_status:
            self.assertEqual(launcher.main(), 0)
            mock_status.assert_called_once()

        with patch("sys.argv", ["launcher.py", "candidates"]), patch("launcher.print_candidates", return_value=0) as mock_cand:
            self.assertEqual(launcher.main(), 0)
            mock_cand.assert_called_once()

        with patch("sys.argv", ["launcher.py", "plan"]), patch("launcher.print_candidates", return_value=0) as mock_plan:
            self.assertEqual(launcher.main(), 0)
            mock_plan.assert_called_once()

        with patch("sys.argv", ["launcher.py", "resize-plan"]), patch("launcher.print_resize_plan", return_value=0) as mock_rp:
            self.assertEqual(launcher.main(), 0)
            mock_rp.assert_called_once()

        with patch("sys.argv", ["launcher.py", "run"]), patch("launcher.run_once", return_value=0) as mock_run:
            self.assertEqual(launcher.main(), 0)
            mock_run.assert_called_once()

        with patch("sys.argv", ["launcher.py", "doctor"]), patch("launcher.doctor", return_value=0) as mock_doc:
            self.assertEqual(launcher.main(), 0)
            mock_doc.assert_called_once_with(False)

    # =========================================================================
    # v1.3.0 RESIZE_ONLY Operating Mode Regression Tests
    # =========================================================================

    @patch("launcher.create_oci_clients")
    def test_v130_resize_only_submits_apply_only_to_resize_stack(self, mock_create_clients: MagicMock) -> None:
        """1. RESIZE_ONLY with 1/6 target instance submits APPLY only against RESIZE_STACK_OCID."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["PROVISIONING_MODE"] = "RESIZE_ONLY"
        os.environ["RESIZE_INSTANCE_OCID"] = "ocid1.instance.purgatory02"
        os.environ["RESIZE_STACK_OCID"] = "ocid1.ormstack.purgatory02_ad3e"
        os.environ["RESIZE_TARGET_OCPUS"] = "2"
        os.environ["RESIZE_TARGET_MEMORY_GB"] = "12"
        os.environ["COMPARTMENT_OCID"] = "ocid1.tenancy.oc1..test"
        os.environ["CONTROL_INSTANCE_OCID"] = "ocid1.instance.control"

        mock_inst = MagicMock()
        mock_inst.id = "ocid1.instance.purgatory02"
        mock_inst.display_name = "purgatory02"
        mock_inst.shape = "VM.Standard.A1.Flex"
        mock_inst.shape_config.ocpus = 1.0
        mock_inst.shape_config.memory_in_gbs = 6.0
        mock_inst.availability_domain = "US-ASHBURN-AD-3"
        mock_inst.lifecycle_state = "RUNNING"

        mock_ctrl = MagicMock()
        mock_ctrl.lifecycle_state = "RUNNING"
        mock_compute.get_instance.side_effect = lambda ocid: MagicMock(data=mock_ctrl) if ocid == "ocid1.instance.control" else MagicMock(data=mock_inst)

        mock_rm.list_jobs.return_value.data = []

        mock_job = MagicMock()
        mock_job.id = "ocid1.ormjob.apply1"
        mock_job.lifecycle_state = "SUCCEEDED"
        mock_rm.create_job.return_value.data = mock_job
        mock_rm.get_job.return_value.data = mock_job
        mock_rm.get_job_logs.return_value.data = []

        with patch("launcher.DRY_RUN", False):
            ret = launcher.run_once()
            self.assertEqual(ret, 0)
            mock_rm.create_job.assert_called_once()
            call_args = mock_rm.create_job.call_args.kwargs.get("create_job_details") or mock_rm.create_job.call_args[0][0]
            self.assertEqual(call_args.stack_id, "ocid1.ormstack.purgatory02_ad3e")
            self.assertEqual(call_args.operation, "APPLY")

    @patch("launcher.create_oci_clients")
    def test_v130_resize_stack_in_successful_stack_ocids_still_usable(self, mock_create_clients: MagicMock) -> None:
        """2. A RESIZE_STACK_OCID present in successful_stack_ocids is still authorized and usable for resize."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["PROVISIONING_MODE"] = "RESIZE_ONLY"
        os.environ["RESIZE_INSTANCE_OCID"] = "ocid1.instance.purgatory02"
        os.environ["RESIZE_STACK_OCID"] = "ocid1.ormstack.purgatory02_ad3e"
        os.environ["COMPARTMENT_OCID"] = "ocid1.tenancy.oc1..test"
        os.environ["CONTROL_INSTANCE_OCID"] = "ocid1.instance.control"

        state = {
            "next_ad_index": 0,
            "successful_stack_ocids": ["ocid1.ormstack.purgatory02_ad3e"],
            "successful_instance_ids": ["ocid1.instance.purgatory02"],
        }
        common.save_state(state)

        mock_inst = MagicMock()
        mock_inst.id = "ocid1.instance.purgatory02"
        mock_inst.shape_config.ocpus = 1.0
        mock_inst.shape_config.memory_in_gbs = 6.0
        mock_inst.lifecycle_state = "RUNNING"

        mock_ctrl = MagicMock()
        mock_ctrl.lifecycle_state = "RUNNING"
        mock_compute.get_instance.side_effect = lambda ocid: MagicMock(data=mock_ctrl) if ocid == "ocid1.instance.control" else MagicMock(data=mock_inst)

        mock_rm.list_jobs.return_value.data = []
        mock_job = MagicMock()
        mock_job.id = "ocid1.ormjob.apply2"
        mock_job.lifecycle_state = "SUCCEEDED"
        mock_rm.create_job.return_value.data = mock_job
        mock_rm.get_job.return_value.data = mock_job
        mock_rm.get_job_logs.return_value.data = []

        with patch("launcher.DRY_RUN", False):
            ret = launcher.run_once()
            self.assertEqual(ret, 0)
            mock_rm.create_job.assert_called_once()
            call_args = mock_rm.create_job.call_args.kwargs.get("create_job_details") or mock_rm.create_job.call_args[0][0]
            self.assertEqual(call_args.stack_id, "ocid1.ormstack.purgatory02_ad3e")

    @patch("launcher.create_oci_clients")
    def test_v130_out_of_host_capacity_no_fallback(self, mock_create_clients: MagicMock) -> None:
        """3 & 4. Out-of-host-capacity during resize causes NO fallback/new-instance submission to AD1/AD2/AD3 stacks."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["PROVISIONING_MODE"] = "RESIZE_ONLY"
        os.environ["RESIZE_INSTANCE_OCID"] = "ocid1.instance.purgatory02"
        os.environ["RESIZE_STACK_OCID"] = "ocid1.ormstack.purgatory02_ad3e"
        os.environ["COMPARTMENT_OCID"] = "ocid1.tenancy.oc1..test"
        os.environ["CONTROL_INSTANCE_OCID"] = "ocid1.instance.control"

        mock_inst = MagicMock()
        mock_inst.id = "ocid1.instance.purgatory02"
        mock_inst.shape_config.ocpus = 1.0
        mock_inst.shape_config.memory_in_gbs = 6.0
        mock_inst.lifecycle_state = "RUNNING"

        mock_ctrl = MagicMock()
        mock_ctrl.lifecycle_state = "RUNNING"
        mock_compute.get_instance.side_effect = lambda ocid: MagicMock(data=mock_ctrl) if ocid == "ocid1.instance.control" else MagicMock(data=mock_inst)

        mock_rm.list_jobs.return_value.data = []

        mock_job = MagicMock()
        mock_job.id = "ocid1.ormjob.apply_failed"
        mock_job.lifecycle_state = "FAILED"
        mock_rm.create_job.return_value.data = mock_job
        mock_rm.get_job.return_value.data = mock_job
        log_entry = MagicMock()
        log_entry.message = "Out of host capacity for shape VM.Standard.A1.Flex"
        mock_rm.get_job_logs.return_value.data = [log_entry]

        with patch("launcher.DRY_RUN", False):
            ret = launcher.run_once()
            self.assertEqual(ret, 0)
            self.assertEqual(mock_rm.create_job.call_count, 1)

    @patch("launcher.create_oci_clients")
    def test_v130_target_reached_marks_complete_no_apply(self, mock_create_clients: MagicMock) -> None:
        """5. Existing instance at 2/12 results in COMPLETE and no APPLY."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["PROVISIONING_MODE"] = "RESIZE_ONLY"
        os.environ["RESIZE_INSTANCE_OCID"] = "ocid1.instance.purgatory02"
        os.environ["RESIZE_STACK_OCID"] = "ocid1.ormstack.purgatory02_ad3e"
        os.environ["COMPARTMENT_OCID"] = "ocid1.tenancy.oc1..test"
        os.environ["CONTROL_INSTANCE_OCID"] = "ocid1.instance.control"

        mock_inst = MagicMock()
        mock_inst.id = "ocid1.instance.purgatory02"
        mock_inst.display_name = "purgatory02"
        mock_inst.shape = "VM.Standard.A1.Flex"
        mock_inst.shape_config.ocpus = 2.0
        mock_inst.shape_config.memory_in_gbs = 12.0
        mock_inst.availability_domain = "US-ASHBURN-AD-3"
        mock_inst.time_created = None
        mock_inst.lifecycle_state = "RUNNING"

        mock_ctrl = MagicMock()
        mock_ctrl.lifecycle_state = "RUNNING"
        mock_compute.get_instance.side_effect = lambda ocid: MagicMock(data=mock_ctrl) if ocid == "ocid1.instance.control" else MagicMock(data=mock_inst)

        with patch("launcher.DRY_RUN", False):
            ret = launcher.run_once()
            self.assertEqual(ret, 0)
            mock_rm.create_job.assert_not_called()
            self.assertTrue(common.COMPLETE_FILE.exists())

    @patch("launcher.create_oci_clients")
    def test_v130_active_job_prevents_duplicate_submission(self, mock_create_clients: MagicMock) -> None:
        """7. Active APPLY job prevents duplicate concurrent submission."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["PROVISIONING_MODE"] = "RESIZE_ONLY"
        os.environ["RESIZE_INSTANCE_OCID"] = "ocid1.instance.purgatory02"
        os.environ["RESIZE_STACK_OCID"] = "ocid1.ormstack.purgatory02_ad3e"
        os.environ["COMPARTMENT_OCID"] = "ocid1.tenancy.oc1..test"
        os.environ["CONTROL_INSTANCE_OCID"] = "ocid1.instance.control"

        mock_inst = MagicMock()
        mock_inst.shape_config.ocpus = 1.0
        mock_inst.shape_config.memory_in_gbs = 6.0
        mock_inst.lifecycle_state = "RUNNING"

        mock_ctrl = MagicMock()
        mock_ctrl.lifecycle_state = "RUNNING"
        mock_compute.get_instance.side_effect = lambda ocid: MagicMock(data=mock_ctrl) if ocid == "ocid1.instance.control" else MagicMock(data=mock_inst)

        active_job = MagicMock()
        active_job.stack_id = "ocid1.ormstack.purgatory02_ad3e"
        active_job.id = "ocid1.ormjob.in_progress"
        active_job.lifecycle_state = "IN_PROGRESS"
        mock_rm.list_jobs.return_value.data = [active_job]

        with patch("launcher.DRY_RUN", False):
            ret = launcher.run_once()
            self.assertEqual(ret, 0)
            mock_rm.create_job.assert_not_called()

    @patch("launcher.create_oci_clients")
    def test_v130_dry_run_submits_nothing(self, mock_create_clients: MagicMock) -> None:
        """8. DRY_RUN=true submits nothing."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["PROVISIONING_MODE"] = "RESIZE_ONLY"
        os.environ["RESIZE_INSTANCE_OCID"] = "ocid1.instance.purgatory02"
        os.environ["RESIZE_STACK_OCID"] = "ocid1.ormstack.purgatory02_ad3e"
        os.environ["COMPARTMENT_OCID"] = "ocid1.tenancy.oc1..test"
        os.environ["CONTROL_INSTANCE_OCID"] = "ocid1.instance.control"

        mock_inst = MagicMock()
        mock_inst.shape_config.ocpus = 1.0
        mock_inst.shape_config.memory_in_gbs = 6.0
        mock_inst.lifecycle_state = "RUNNING"

        mock_ctrl = MagicMock()
        mock_ctrl.lifecycle_state = "RUNNING"
        mock_compute.get_instance.side_effect = lambda ocid: MagicMock(data=mock_ctrl) if ocid == "ocid1.instance.control" else MagicMock(data=mock_inst)
        mock_rm.list_jobs.return_value.data = []

        with patch("launcher.DRY_RUN", True):
            ret = launcher.run_once()
            self.assertEqual(ret, 0)
            mock_rm.create_job.assert_not_called()

    @patch("launcher.create_oci_clients")
    def test_v130_resize_plan_is_read_only(self, mock_create_clients: MagicMock) -> None:
        """9 & 11. resize-plan is strictly read-only and calls NO capacity report API."""
        mock_compute = MagicMock()
        mock_rm = MagicMock()
        mock_create_clients.return_value = (mock_compute, mock_rm)

        os.environ["PROVISIONING_MODE"] = "RESIZE_ONLY"
        os.environ["RESIZE_INSTANCE_OCID"] = "ocid1.instance.purgatory02"
        os.environ["RESIZE_STACK_OCID"] = "ocid1.ormstack.purgatory02_ad3e"
        os.environ["COMPARTMENT_OCID"] = "ocid1.tenancy.oc1..test"
        os.environ["CONTROL_INSTANCE_OCID"] = "ocid1.instance.control"

        mock_inst = MagicMock()
        mock_inst.display_name = "purgatory02"
        mock_inst.shape_config.ocpus = 1.0
        mock_inst.shape_config.memory_in_gbs = 6.0
        mock_compute.get_instance.return_value.data = mock_inst

        mock_stk = MagicMock()
        mock_stk.display_name = "purgatory02-ad3e"
        mock_rm.get_stack.return_value.data = mock_stk
        mock_rm.list_jobs.return_value.data = []

        plan = launcher.get_resize_plan()
        self.assertEqual(plan["mode"], "RESIZE_ONLY")
        self.assertTrue(plan["is_configured"])
        self.assertFalse(plan["complete"])
        self.assertEqual(plan["instance"]["ocpus"], 1.0)
        self.assertEqual(plan["target_shape"]["ocpus"], 2.0)

        # Confirm NO mutating calls or capacity report calls were made
        mock_rm.create_job.assert_not_called()
        mock_compute.create_compute_capacity_report.assert_not_called()

        # Confirm print_resize_plan executes cleanly
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            ret = launcher.print_resize_plan()
            self.assertEqual(ret, 0)
            out = fake_out.getvalue()
            self.assertIn("Provisioning Mode: RESIZE_ONLY", out)
            self.assertIn("Target:  2 OCPU / 12 GB RAM", out)

    def test_v130_missing_or_placeholder_config_fails_safe(self) -> None:
        """10. Missing/placeholder RESIZE_* settings fail safe."""
        os.environ["PROVISIONING_MODE"] = "RESIZE_ONLY"
        os.environ["RESIZE_INSTANCE_OCID"] = "REPLACE_WITH_EXISTING_INSTANCE_OCID"
        os.environ["RESIZE_STACK_OCID"] = "REPLACE_WITH_EXISTING_STACK_OCID"

        cfg = common.get_resize_config()
        self.assertFalse(cfg["is_configured"])

        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            ret = launcher.doctor(False)
            self.assertEqual(ret, 1)
            out = fake_out.getvalue()
            self.assertIn("ERROR: PROVISIONING_MODE=RESIZE_ONLY is set", out)

        plan = launcher.get_resize_plan()
        self.assertFalse(plan["is_configured"])
        self.assertIn("FAILSAFE", plan["status_text"])


if __name__ == "__main__":
    unittest.main()
