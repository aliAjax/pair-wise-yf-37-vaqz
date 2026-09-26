import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ContactConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.investigator = Actor("inv-1", "investigator")
        self.case = self.service.create(self.admin, "case", {
            "person_id": "P-1", "onset_date": "2026-03-01",
            "location": "District-A", "symptoms": ["fever"],
        })
        self.contact = self.service.create(self.investigator, "contact", {
            "case_id": self.case["id"], "person_id": "P-2", "exposure_start": "2026-02-25",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self, result, **data):
        data["result"] = result
        return self.service.transition(
            self.investigator, self.contact["id"], "record_contact", data
        )

    def _reopen(self):
        return self.service.transition(
            self.investigator, self.contact["id"], "reopen_contact", {}
        )

    def _update_exposure(self, exposure_date):
        return self.service.transition(
            self.investigator, self.case["id"], "update_exposure",
            {"exposure_date": exposure_date},
        )

    def test_answered_keeps_first_confirmation_time(self):
        first = self._record("answered", at="2026-03-02T09:00:00+00:00")
        self.assertEqual(first["status"], "identified")
        self.assertEqual(first["data"]["confirmed_at"], "2026-03-02T09:00:00+00:00")
        second = self._record("answered", at="2026-03-03T09:00:00+00:00")
        self.assertEqual(second["data"]["confirmed_at"], "2026-03-02T09:00:00+00:00")
        self.assertEqual(second["data"]["last_contact_at"], "2026-03-03T09:00:00+00:00")
        self.assertEqual(len(second["data"]["contact_log"]), 2)
        self.assertEqual(second["data"]["contact_log"][0]["result"], "answered")

    def test_unanswered_requeues_from_attempt_day(self):
        updated = self._record("unanswered", at="2026-03-02T22:30:00+00:00")
        self.assertEqual(updated["status"], "identified")
        self.assertEqual(updated["data"]["queued_from"], "2026-03-02")
        queue = self.service.contact_queue()
        self.assertEqual(queue["pending_count"], 1)
        item = queue["items"][0]
        self.assertTrue(item["pending"])
        self.assertEqual(item["last_result"], "unanswered")
        self.assertEqual(item["queued_from"], "2026-03-02")

    def test_refused_requires_reason(self):
        with self.assertRaises(ValidationError):
            self._record("refused")

    def test_unknown_result_rejected(self):
        with self.assertRaises(ValidationError):
            self._record("no_answer")

    def test_refused_stops_followup_and_shows_reason(self):
        refused = self._record("refused", reason="拒绝随访", at="2026-03-02T10:00:00+00:00")
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["data"]["refusal_reason"], "拒绝随访")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.investigator, self.contact["id"], "begin_followup",
                {"followup_start": "2026-03-02", "due_at": "2026-03-16"},
            )
        with self.assertRaises(InvalidTransition):
            self._record("answered")
        queue = self.service.contact_queue()
        self.assertEqual(queue["pending_count"], 0)
        item = queue["items"][0]
        self.assertFalse(item["pending"])
        self.assertEqual(item["last_result"], "refused")
        self.assertEqual(item["refusal_reason"], "拒绝随访")

    def test_reopen_only_with_later_case_exposure(self):
        self._record("refused", reason="拒绝随访")
        with self.assertRaises(ValidationError):
            self._reopen()
        self._update_exposure("2026-02-20")
        with self.assertRaises(ValidationError):
            self._reopen()
        self._update_exposure("2026-03-05")
        reopened = self._reopen()
        self.assertEqual(reopened["status"], "identified")
        self.assertEqual(reopened["data"]["exposure_date"], "2026-03-05")
        again = self._record("answered", at="2026-03-06T08:00:00+00:00")
        self.assertEqual(again["status"], "identified")
        self.assertEqual(again["data"]["confirmed_at"], "2026-03-06T08:00:00+00:00")

    def test_queue_flags_reopenable_after_later_exposure(self):
        self._record("refused", reason="拒绝随访")
        queue = self.service.contact_queue()
        self.assertFalse(queue["items"][0]["reopenable"])
        self._update_exposure("2026-03-05")
        queue = self.service.contact_queue()
        self.assertTrue(queue["items"][0]["reopenable"])

    def test_completed_contact_reopens_with_later_exposure(self):
        self.service.transition(
            self.investigator, self.contact["id"], "begin_followup",
            {"followup_start": "2026-03-02", "due_at": "2026-03-16"},
        )
        self.service.transition(
            self.investigator, self.contact["id"], "complete_followup",
            {"outcome": "no symptoms"},
        )
        with self.assertRaises(InvalidTransition):
            self._record("unanswered")
        self._update_exposure("2026-03-05")
        reopened = self._reopen()
        self.assertEqual(reopened["status"], "identified")

    def test_update_exposure_must_move_forward(self):
        self._update_exposure("2026-03-05")
        with self.assertRaises(ValidationError):
            self._update_exposure("2026-03-01")
        with self.assertRaises(ValidationError):
            self._update_exposure("not-a-date")

    def test_record_contact_requires_investigator_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"), self.contact["id"],
                "record_contact", {"result": "answered"},
            )


if __name__ == "__main__":
    unittest.main()
