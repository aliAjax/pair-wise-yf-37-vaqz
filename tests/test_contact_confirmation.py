import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
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

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id="P-1", onset="2026-03-01"):
        return self.service.create(self.admin, "case", {
            "person_id": person_id,
            "onset_date": onset,
            "location": "A",
            "symptoms": ["fever"],
        })

    def _contact(self, case_id, person_id="P-2", exposure_start="2026-02-25"):
        return self.service.create(self.investigator, "contact", {
            "case_id": case_id,
            "person_id": person_id,
            "exposure_start": exposure_start,
        })

    def test_results_logged_separately_and_first_time_preserved(self):
        case = self._case()
        contact = self._contact(case["id"])
        first = self.service.transition(
            self.investigator, contact["id"], "contact_answered",
            {"contacted_at": "2026-03-02T09:00:00+00:00"},
        )
        self.assertEqual(first["status"], "following")
        self.assertEqual(first["data"]["first_contacted_at"], "2026-03-02T09:00:00+00:00")
        second = self.service.transition(
            self.investigator, contact["id"], "contact_no_answer",
            {"contacted_at": "2026-03-02T18:30:00+00:00"},
        )
        self.assertEqual(second["status"], "queued")
        self.assertEqual(second["data"]["queued_on"], "2026-03-02")
        # 重复确认不覆盖已有的首次联系时间
        self.assertEqual(second["data"]["first_contacted_at"], "2026-03-02T09:00:00+00:00")
        log = second["data"]["contact_log"]
        self.assertEqual([entry["result"] for entry in log], ["answered", "no_answer"])
        self.assertEqual(log[0]["at"], "2026-03-02T09:00:00+00:00")
        self.assertEqual(log[1]["at"], "2026-03-02T18:30:00+00:00")
        self.assertEqual(second["data"]["last_contact_result"], "no_answer")
        self.assertEqual(second["data"]["contact_attempts"], 2)

    def test_refused_stops_followup_and_keeps_reason(self):
        case = self._case()
        contact = self._contact(case["id"])
        self.service.transition(
            self.investigator, contact["id"], "begin_followup",
            {"followup_start": "2026-03-02", "due_at": "2026-03-16"},
        )
        refused = self.service.transition(
            self.investigator, contact["id"], "contact_refused",
            {"reason": "拒绝接受流调", "contacted_at": "2026-03-03T10:00:00+00:00"},
        )
        self.assertEqual(refused["status"], "refused")
        self.assertTrue(refused["data"]["followup_stopped"])
        self.assertFalse(refused["data"]["followup_todo"])
        self.assertEqual(refused["data"]["refusal_reason"], "拒绝接受流调")
        self.assertIsNone(refused["data"]["due_at"])
        # 拒访后不再产生随访待办，也不能再直接确认
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.investigator, contact["id"], "complete_followup", {"outcome": "done"}
            )
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.investigator, contact["id"], "contact_answered", {})

    def test_refused_requires_reason(self):
        case = self._case()
        contact = self._contact(case["id"])
        with self.assertRaises(ValidationError):
            self.service.transition(self.investigator, contact["id"], "contact_refused", {})

    def test_reopen_only_with_later_exposure_on_linked_case(self):
        case = self._case(onset="2026-03-01")
        contact = self._contact(case["id"], exposure_start="2026-03-02")
        self.service.transition(
            self.investigator, contact["id"], "contact_refused", {"reason": "拒绝"}
        )
        # 关联病例没有更晚暴露日时不允许重新开放
        with self.assertRaises(ValidationError):
            self.service.transition(self.investigator, contact["id"], "reopen_contact", {})
        # 关联病例出现更晚暴露日
        self.service.transition(
            self.admin, case["id"], "triage",
            {"clinician": "C-1", "exposure_date": "2026-03-05"},
        )
        reopened = self.service.transition(
            self.investigator, contact["id"], "reopen_contact", {}
        )
        self.assertEqual(reopened["status"], "following")
        self.assertFalse(reopened["data"]["followup_stopped"])
        self.assertTrue(reopened["data"]["followup_todo"])
        self.assertEqual(reopened["data"]["exposure_start"], "2026-03-05")
        # 历史确认记录保留，重新开放后可以继续联系确认
        self.assertEqual(len(reopened["data"]["contact_log"]), 1)
        again = self.service.transition(
            self.investigator, reopened["id"], "contact_answered",
            {"contacted_at": "2026-03-06T09:00:00+00:00"},
        )
        self.assertEqual(len(again["data"]["contact_log"]), 2)
        self.assertEqual(again["data"]["last_contact_result"], "answered")

    def test_board_pending_count_and_display_fields(self):
        case = self._case()
        pending_new = self._contact(case["id"], person_id="P-2")
        refused = self._contact(case["id"], person_id="P-3")
        self.service.transition(
            self.investigator, refused["id"], "contact_refused", {"reason": "拒绝随访"}
        )
        queued = self._contact(case["id"], person_id="P-4")
        self.service.transition(
            self.investigator, queued["id"], "contact_no_answer",
            {"contacted_at": "2026-03-02T08:00:00+00:00"},
        )
        board = self.service.contact_board()
        self.assertEqual(board["pending_count"], 2)
        items = {item["id"]: item for item in board["items"]}
        self.assertTrue(items[pending_new["id"]]["pending"])
        self.assertEqual(items[refused["id"]]["last_result"], "refused")
        self.assertEqual(items[refused["id"]]["refusal_reason"], "拒绝随访")
        self.assertFalse(items[refused["id"]]["pending"])
        self.assertEqual(items[queued["id"]]["last_result"], "no_answer")
        self.assertEqual(items[queued["id"]]["queued_on"], "2026-03-02")
        self.assertTrue(items[queued["id"]]["pending"])


if __name__ == "__main__":
    unittest.main()
