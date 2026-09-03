import dataclasses
import unittest

from acp.errors import TrafficClass
from acp.jobs import Job, JobState, JobTransitionError, is_terminal, transition


def _make_job(state: JobState = JobState.QUEUED) -> Job:
    return Job(
        job_id="job-1",
        source_hash="a" * 64,
        flow_id=None,
        traffic_class=TrafficClass.GENERAL,
        urgency_class="normal",
        created_at=0.0,
        started_at=None,
        completed_at=None,
        result_ref=None,
        result_hash=None,
        state=state,
        policy_version="v1",
    )


class JobFieldsTest(unittest.TestCase):
    def test_no_credential_like_field_present(self) -> None:
        field_names = {field.name for field in dataclasses.fields(Job)}
        forbidden_substrings = ("credential", "secret", "token", "password", "key")
        suspicious = {
            name for name in field_names
            if any(bad in name.lower() for bad in forbidden_substrings)
        }
        self.assertEqual(suspicious, set())

    def test_expected_fields_present(self) -> None:
        field_names = {field.name for field in dataclasses.fields(Job)}
        self.assertEqual(
            field_names,
            {
                "job_id", "source_hash", "flow_id",
                "traffic_class", "urgency_class",
                "created_at", "started_at", "completed_at", "result_ref",
                "result_hash", "state", "policy_version",
            },
        )

    def test_flow_id_optional(self) -> None:
        job = _make_job()
        self.assertIsNone(job.flow_id)


class JobStateTest(unittest.TestCase):
    def test_exactly_ten_states(self) -> None:
        names = {member.name for member in JobState}
        self.assertEqual(
            names,
            {
                "QUEUED", "RUNNING", "READY", "FAILED", "QUEUE_TIMEOUT",
                "COMPRESSION_TIMEOUT", "TOTAL_TIMEOUT", "BYPASSED", "BLOCKED",
                "STALE",
            },
        )


class TransitionTest(unittest.TestCase):
    def test_queued_to_running_allowed(self) -> None:
        job = _make_job(JobState.QUEUED)
        transition(job, JobState.RUNNING)
        self.assertEqual(job.state, JobState.RUNNING)

    def test_running_to_ready_allowed(self) -> None:
        job = _make_job(JobState.RUNNING)
        transition(job, JobState.READY)
        self.assertEqual(job.state, JobState.READY)

    def test_running_to_failed_allowed(self) -> None:
        job = _make_job(JobState.RUNNING)
        transition(job, JobState.FAILED)
        self.assertEqual(job.state, JobState.FAILED)

    def test_queued_to_queue_timeout_allowed(self) -> None:
        job = _make_job(JobState.QUEUED)
        transition(job, JobState.QUEUE_TIMEOUT)
        self.assertEqual(job.state, JobState.QUEUE_TIMEOUT)

    def test_ready_to_stale_allowed(self) -> None:
        job = _make_job(JobState.READY)
        transition(job, JobState.STALE)
        self.assertEqual(job.state, JobState.STALE)

    def test_queued_to_ready_rejected(self) -> None:
        job = _make_job(JobState.QUEUED)
        with self.assertRaises(JobTransitionError):
            transition(job, JobState.READY)

    def test_terminal_state_rejects_all_transitions(self) -> None:
        for state in (
            JobState.FAILED, JobState.QUEUE_TIMEOUT, JobState.COMPRESSION_TIMEOUT,
            JobState.TOTAL_TIMEOUT, JobState.BYPASSED, JobState.BLOCKED,
        ):
            job = _make_job(state)
            with self.assertRaises(JobTransitionError):
                transition(job, JobState.RUNNING)

    def test_stale_is_terminal(self) -> None:
        job = _make_job(JobState.STALE)
        with self.assertRaises(JobTransitionError):
            transition(job, JobState.READY)

    def test_job_unchanged_on_rejected_transition(self) -> None:
        job = _make_job(JobState.QUEUED)
        with self.assertRaises(JobTransitionError):
            transition(job, JobState.READY)
        self.assertEqual(job.state, JobState.QUEUED)


class IsTerminalTest(unittest.TestCase):
    def test_queued_not_terminal(self) -> None:
        self.assertFalse(is_terminal(JobState.QUEUED))

    def test_running_not_terminal(self) -> None:
        self.assertFalse(is_terminal(JobState.RUNNING))

    def test_ready_is_terminal(self) -> None:
        self.assertTrue(is_terminal(JobState.READY))

    def test_failed_is_terminal(self) -> None:
        self.assertTrue(is_terminal(JobState.FAILED))


if __name__ == "__main__":
    unittest.main()
