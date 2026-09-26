from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _contact_log_extra(actor, entity, data, result):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log = list(entity["data"].get("contact_log", []))
    entry = {
        "at": str(data.get("contacted_at") or now),
        "result": result,
        "by": actor.user_id,
    }
    if data.get("reason"):
        entry["reason"] = data["reason"]
    log.append(entry)
    extra = {
        "contact_log": log,
        "contact_attempts": len(log),
        "last_contact_result": result,
        "last_contacted_at": entry["at"],
    }
    # 重复确认只追加新记录，不覆盖已有的首次联系时间
    if not entity["data"].get("first_contacted_at"):
        extra["first_contacted_at"] = entry["at"]
    return extra


def _validate_contact_answered(actor, entity, data, lookup):
    return _contact_log_extra(actor, entity, data, "answered")


def _validate_contact_no_answer(actor, entity, data, lookup):
    extra = _contact_log_extra(actor, entity, data, "no_answer")
    # 未接听从联系当天重新排队
    extra["queued_on"] = extra["last_contacted_at"][:10]
    return extra


def _validate_contact_refused(actor, entity, data, lookup):
    if not data.get("reason"):
        raise ValidationError("refused contact requires a reason")
    extra = _contact_log_extra(actor, entity, data, "refused")
    # 拒访后停止随访待办并保留原因
    extra.update({
        "refusal_reason": data["reason"],
        "followup_stopped": True,
        "followup_todo": False,
        "due_at": None,
    })
    return extra


def _validate_reopen_contact(actor, entity, data, lookup):
    case_id = entity["data"].get("case_id")
    case = _find_one(lookup, "case", "id", case_id) if case_id else None
    if not case:
        raise ValidationError("linked case not found for contact")
    case_exposure = case["data"].get("exposure_date") or case["data"].get("onset_date")
    current_start = entity["data"].get("exposure_start")
    if not case_exposure or not current_start:
        raise ValidationError("exposure dates are required to reopen contact")
    if _date_ordinal(case_exposure) <= _date_ordinal(current_start):
        raise ValidationError("reopen requires a later exposure date on the linked case")
    # 关联病例出现更晚暴露日：重新开放联系和随访
    return {
        "exposure_start": str(case_exposure)[:10],
        "followup_stopped": False,
        "followup_todo": True,
        "reopened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reopen_reason": data.get("reason") or "later exposure date on linked case",
    }


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


CUSTOM_CREATE = {'case': _validate_case}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable, ('contact', 'contact_answered'): _validate_contact_answered, ('contact', 'contact_no_answer'): _validate_contact_no_answer, ('contact', 'contact_refused'): _validate_contact_refused, ('contact', 'reopen_contact'): _validate_reopen_contact}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed'), 'contact_answered': (('identified', 'following', 'queued'), 'following'), 'contact_no_answer': (('identified', 'following', 'queued'), 'queued'), 'contact_refused': (('identified', 'following', 'queued'), 'refused'), 'reopen_contact': (('refused', 'completed'), 'following')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',), ('contact', 'contact_refused'): ('reason',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator'), 'contact_answered': ('admin', 'investigator'), 'contact_no_answer': ('admin', 'investigator'), 'contact_refused': ('admin', 'investigator'), 'reopen_contact': ('admin', 'investigator')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
