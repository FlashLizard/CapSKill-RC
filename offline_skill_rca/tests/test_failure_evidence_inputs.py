"""失败结果与最终产物可见性边界的回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import (
    AGENT_ONLY_EVIDENCE_POLICY_VERSION,
    discover_rollout_selection,
    load_trajectory,
    sanitize_agent_artifacts,
    sanitize_agent_only_visible_result,
)
from src.pipeline import sanitize_trajectory_for_llm, validate_failed_trajectories
from src.llm_client import extract_json_object
from src.stages.stage_03_failure_event_extraction import build_prompt as build_stage3_prompt
from src.stages.stage_03_failure_event_extraction import run_one as run_stage3_one
from src.stages.stage_03_failure_event_extraction import stage3_trajectory_input


class FailureEvidenceInputTests(unittest.TestCase):
    def test_rollout_selection_filters_non_task_failures_before_applying_cap(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            cases = [
                ("01-success", {"success": 1}),
                ("02-timeout", {"success": 0, "error_category": "agent_timeout"}),
                ("03-sandbox", {"success": 0, "error": "sandbox startup failed"}),
                ("04-failure-a", {"success": 0}),
                ("05-missing-outcome", {"error": "rollout was interrupted"}),
                ("06-failure-b", {"rewards": {"score": 0}}),
                ("07-failure-c", {"success": 0}),
            ]
            for name, result in cases:
                rollout = root / name
                (rollout / "trajectory").mkdir(parents=True)
                (rollout / "trajectory" / "acp_trajectory.jsonl").write_text(
                    '{"type":"agent_message","text":"failed"}\n',
                    encoding="utf-8",
                )
                (rollout / "result.json").write_text(json.dumps(result), encoding="utf-8")

            selected, records = discover_rollout_selection([root], max_traces=2)

            self.assertEqual([path.name for path in selected], ["04-failure-a", "06-failure-b"])
            by_name = {Path(item["rolloutDir"]).name: item for item in records}
            self.assertEqual(by_name["01-success"]["reason"], "success")
            self.assertEqual(by_name["02-timeout"]["reason"], "timeout")
            self.assertEqual(by_name["03-sandbox"]["reason"], "environment_or_configuration_error")
            self.assertEqual(by_name["05-missing-outcome"]["reason"], "missing_explicit_outcome")
            self.assertTrue(by_name["04-failure-a"]["selectedForRepair"])
            self.assertFalse(by_name["07-failure-c"]["selectedForRepair"])

    def test_pipeline_rejects_any_authoritatively_successful_trajectory(self) -> None:
        trajectories = [
            {"traj_id": "failed", "success": 0, "visible_failure_result": {"success": 0}},
            {"traj_id": "passed", "success": 1, "visible_failure_result": {"success": 1}},
        ]

        with self.assertRaisesRegex(SystemExit, "passed"):
            validate_failed_trajectories(trajectories)

    def test_pipeline_accepts_only_failed_trajectories(self) -> None:
        validate_failed_trajectories(
            [
                {"traj_id": "T1", "success": 0, "visible_failure_result": {"success": 0}},
                {"traj_id": "T2", "success": 0, "visible_failure_result": {"success": 0}},
            ]
        )

    def test_balanced_json_extraction_ignores_fences_and_extra_closing_brace(self) -> None:
        text = '```json\n{"outer":{"text":"a } brace", "value":1}}}\n```'

        extracted = extract_json_object(text)

        self.assertEqual({"outer": {"text": "a } brace", "value": 1}}, json.loads(extracted))

    def test_verifier_data_is_excluded_while_agent_artifacts_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            rollout = root / "jobs" / "task__run"
            (rollout / "trajectory").mkdir(parents=True)
            (rollout / "verifier").mkdir()
            (rollout / "artifacts").mkdir()
            trajectory_events = [
                {"type": "agent_message", "text": "created output.csv"},
                {
                    "type": "tool_call",
                    "kind": "edit",
                    "title": "Write",
                    "status": "completed",
                    "content": [
                        {
                            "type": "diff",
                            "path": "/root/workspace/solution.py",
                            "oldText": None,
                            "newText": "print('bad result')\n",
                        }
                    ],
                },
            ]
            (rollout / "trajectory" / "acp_trajectory.jsonl").write_text(
                "\n".join(json.dumps(event) for event in trajectory_events) + "\n",
                encoding="utf-8",
            )
            (rollout / "result.json").write_text(
                json.dumps(
                    {
                        "task_name": "task",
                        "success": False,
                        "error": "agent runtime failed",
                        "verifier_error": "one assertion failed",
                        "verifier_timeout_info": {"seconds": 300},
                        "final_metrics": {"hidden_score": 0.25},
                        "rewards": {"reward": 0},
                    }
                ),
                encoding="utf-8",
            )
            (rollout / "verifier" / "test-stdout.txt").write_text(
                "Agent's solution.py:\nprint('secret full file')\nExpected: hidden answer\nGot: wrong\n"
                "[FAILED] row-count check failed\nFINAL SCORE: 0/1 = 0.0\n",
                encoding="utf-8",
            )
            (rollout / "verifier" / "ctrf.json").write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 1, "passed": 0, "failed": 1, "skipped": 0},
                            "tests": [
                                {
                                    "name": "::test_row_count",
                                    "status": "failed",
                                    "file_path": "../verifier/test_outputs.py",
                                    "trace": "/verifier/test_outputs.py:10: in test_row_count\n"
                                    "    assert secret_expected == actual\n"
                                    "E   AssertionError: row count mismatch",
                                    "message": "The test failed due to an assertion error",
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (rollout / "artifacts" / "output.csv").write_text("id,value\n1,bad\n", encoding="utf-8")

            trajectory = load_trajectory(root, rollout)

            self.assertEqual("agent runtime failed", trajectory.result["error"])
            self.assertEqual(0, trajectory.result["success"])
            self.assertNotIn("final_metrics", trajectory.result)
            self.assertNotIn("rewards", trajectory.result)
            self.assertNotIn("verifier_error", trajectory.result)
            self.assertNotIn("verifier_timeout_info", trajectory.result)
            self.assertNotIn("failure_outputs", trajectory.result)
            self.assertEqual(
                AGENT_ONLY_EVIDENCE_POLICY_VERSION,
                trajectory.result["evidence_policy"]["version"],
            )
            archived = next(item for item in trajectory.final_artifacts if Path(item["path"]).name == "output.csv")
            reconstructed = next(item for item in trajectory.final_artifacts if item["path"] == "/root/workspace/solution.py")
            self.assertIn("1,bad", archived["content"])
            self.assertEqual("archived_complete", archived["content_status"])
            self.assertIn("bad result", reconstructed["content"])
            self.assertEqual("trajectory_reconstructed_complete", reconstructed["content_status"])

    def test_old_bundle_trajectory_is_refreshed_from_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            rollout = root / "jobs" / "task__old"
            (rollout / "trajectory").mkdir(parents=True)
            (rollout / "verifier").mkdir()
            (rollout / "trajectory" / "acp_trajectory.jsonl").write_text(
                json.dumps({"type": "agent_message", "text": "finished with an error"}) + "\n",
                encoding="utf-8",
            )
            (rollout / "result.json").write_text(
                json.dumps({"task_name": "task", "success": False, "error": "runtime failed"}),
                encoding="utf-8",
            )
            (rollout / "verifier" / "test-stdout.txt").write_text(
                "ERROR test failed during execution\n",
                encoding="utf-8",
            )
            old_bundle_trajectory = sanitize_trajectory_for_llm(load_trajectory(root, rollout))
            old_bundle_trajectory.pop("visible_failure_result")
            old_bundle_trajectory.pop("final_artifacts")
            old_bundle_trajectory["rollout_dir"] = "jobs/task__old"

            refreshed = stage3_trajectory_input(SimpleNamespace(root=root), old_bundle_trajectory)

            self.assertEqual("runtime failed", refreshed["visible_failure_result"]["error"])
            self.assertNotIn("failure_outputs", refreshed["visible_failure_result"])
            self.assertNotIn("verifier_error", refreshed["visible_failure_result"])

    def test_cached_visible_result_is_stripped_without_a_rollout(self) -> None:
        cached = {
            "success": 0,
            "error": "agent timeout",
            "verifier_error": "secret verifier detail",
            "verifier_timeout_info": {"seconds": 300},
            "failure_outputs": [{"content": "hidden test output"}],
            "rewards": {"reward": 0},
            "final_metrics": {"score": 0},
        }

        cleaned = sanitize_agent_only_visible_result(cached)

        self.assertEqual("agent timeout", cleaned["error"])
        self.assertEqual(0, cleaned["success"])
        self.assertNotIn("verifier_error", cleaned)
        self.assertNotIn("verifier_timeout_info", cleaned)
        self.assertNotIn("failure_outputs", cleaned)
        self.assertNotIn("rewards", cleaned)
        self.assertNotIn("final_metrics", cleaned)
        self.assertEqual(AGENT_ONLY_EVIDENCE_POLICY_VERSION, cleaned["evidence_policy"]["version"])

    def test_cached_verifier_artifacts_are_rejected(self) -> None:
        cleaned = sanitize_agent_artifacts(
            [
                {"path": "jobs/run/verifier/test-stdout.txt", "capture_source": "archived_artifact"},
                {"path": "output.csv", "capture_source": "sanitized_verifier_output"},
                {"path": "jobs/run/artifacts/output.csv", "capture_source": "archived_artifact"},
            ]
        )

        self.assertEqual(1, len(cleaned))
        self.assertEqual("jobs/run/artifacts/output.csv", cleaned[0]["path"])

    def test_stage3_prompt_defensively_strips_injected_verifier_data(self) -> None:
        trajectory = {
            "traj_id": "T1",
            "task_id": "task",
            "success": 0,
            "steps": [{"step_id": 1, "action_summary": "agent action"}],
            "verifier_error": "TOP_LEVEL_VERIFIER_SECRET",
            "visible_failure_result": {
                "success": 0,
                "error": "agent runtime error",
                "verifier_error": "VISIBLE_VERIFIER_SECRET",
                "failure_outputs": [{"content": "VERIFIER_OUTPUT_SECRET"}],
            },
            "final_artifacts": [
                {"path": "verifier/ctrf.json", "content": "VERIFIER_ARTIFACT_SECRET"},
                {"path": "artifacts/output.txt", "content": "agent bad output", "capture_source": "archived_artifact"},
            ],
        }

        prompt = build_stage3_prompt({}, {"task_id": "task"}, [], {}, trajectory, 100_000)

        self.assertIn("agent runtime error", prompt)
        self.assertIn("agent bad output", prompt)
        self.assertIn("success=0 is authoritative: the rollout failed", prompt)
        self.assertIn("Never reassess it", prompt)
        self.assertNotIn("TOP_LEVEL_VERIFIER_SECRET", prompt)
        self.assertNotIn("VISIBLE_VERIFIER_SECRET", prompt)
        self.assertNotIn("VERIFIER_OUTPUT_SECRET", prompt)
        self.assertNotIn("VERIFIER_ARTIFACT_SECRET", prompt)

    def test_stage3_persists_agent_only_marker_in_canonical_parsed_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            output_dir = Path(raw_root)
            config = SimpleNamespace(root=output_dir, output_dir=output_dir, max_prompt_chars=100_000)
            trajectory = {
                "traj_id": "T1",
                "task_id": "task",
                "success": 0,
                "steps": [{"step_id": 1, "action_summary": "agent action"}],
                "visible_failure_result": {"success": 0},
                "final_artifacts": [],
            }
            fake_client = SimpleNamespace(
                chat_json=lambda *_args, **_kwargs: {
                    "failure_events": [],
                    "cause_events": [],
                    "causal_links": [],
                    "evidence_limits": [],
                }
            )

            with patch("src.stages.stage_03_failure_event_extraction.make_llm", return_value=fake_client):
                result = run_stage3_one(config, {}, {"task_id": "task"}, [], {}, 0, trajectory)

            parsed_path = output_dir / "llm_transcript" / "stage-03-traj-01-T1.parsed.json"
            persisted = json.loads(parsed_path.read_text(encoding="utf-8"))
            self.assertEqual(AGENT_ONLY_EVIDENCE_POLICY_VERSION, result["evidence_policy_version"])
            self.assertEqual(AGENT_ONLY_EVIDENCE_POLICY_VERSION, persisted["evidence_policy_version"])


if __name__ == "__main__":
    unittest.main()
