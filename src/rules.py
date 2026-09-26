from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


CONTACT_RESULTS = ("answered", "unanswered", "refused")
PENDING_CONTACT_STATUSES = ("identified", "following")
REOPENABLE_CONTACT_STATUSES = ("refused", "completed")


def contact_exposure_date(data):
    return data.get("exposure_date") or data.get("exposure_start")


def is_contact_reopenable(contact, case):
    if not case or contact["status"] not in REOPENABLE_CONTACT_STATUSES:
        return False
    case_exposure = case["data"].get("exposure_date")
    current = contact_exposure_date(contact["data"])
    if not case_exposure or not current:
        return False
    return _date_ordinal(case_exposure) > _date_ordinal(current)


def _validate_update_exposure(actor, entity, data, lookup):
    try:
        new_ordinal = _date_ordinal(data.get("exposure_date"))
    except ValueError:
        raise ValidationError("exposure_date must be an ISO date")
    current = entity["data"].get("exposure_date")
    if current and new_ordinal <= _date_ordinal(current):
        raise ValidationError("exposure_date must be later than the existing one")


def _validate_record_contact(actor, entity, data, lookup):
    result = str(data.get("result", "")).strip().lower()
    if result not in CONTACT_RESULTS:
        raise ValidationError("result must be one of: " + ", ".join(CONTACT_RESULTS))
    reason = str(data.get("reason") or "").strip()
    if result == "refused" and not reason:
        raise ValidationError("refused result requires a reason")
    attempt_at = str(data.get("at") or _now())
    try:
        _date_ordinal(attempt_at)
    except ValueError:
        raise ValidationError("at must be an ISO date or datetime")
    stored = entity["data"]
    log = list(stored.get("contact_log") or [])
    entry = {"at": attempt_at, "result": result, "by": actor.user_id}
    if reason:
        entry["reason"] = reason
    log.append(entry)
    patch = {
        "contact_log": log,
        "last_result": result,
        "last_contact_at": attempt_at,
    }
    next_status = None
    if result == "answered":
        if not stored.get("confirmed_at"):
            patch["confirmed_at"] = attempt_at
    elif result == "unanswered":
        patch["queued_from"] = attempt_at[:10]
    else:
        patch["refusal_reason"] = reason
        if not stored.get("refused_at"):
            patch["refused_at"] = attempt_at
        next_status = "refused"
    return {"patch": patch, "next_status": next_status}


def _validate_reopen_contact(actor, entity, data, lookup):
    case = _find_one(lookup, "case", "id", entity["data"].get("case_id"))
    if not case:
        raise ValidationError("linked case not found: " + str(entity["data"].get("case_id")))
    case_exposure = case["data"].get("exposure_date")
    current = contact_exposure_date(entity["data"])
    if not case_exposure or (current and _date_ordinal(case_exposure) <= _date_ordinal(current)):
        raise ValidationError("reopen requires a later exposure date on the linked case")
    reopened_at = _now()
    patch = {
        "exposure_date": case_exposure,
        "reopened_at": reopened_at,
        "queued_from": reopened_at[:10],
    }
    if data.get("reason"):
        patch["reopen_reason"] = data["reason"]
    return {"patch": patch}


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
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable, ('case', 'update_exposure'): _validate_update_exposure, ('contact', 'record_contact'): _validate_record_contact, ('contact', 'reopen_contact'): _validate_reopen_contact}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed'), 'update_exposure': (('reported', 'investigating', 'confirmed', 'probable'), None)}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed'), 'record_contact': (('identified', 'following'), None), 'reopen_contact': (('refused', 'completed'), 'identified')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('case', 'update_exposure'): ('exposure_date',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',), ('contact', 'record_contact'): ('result',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'update_exposure': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator'), 'record_contact': ('admin', 'investigator'), 'reopen_contact': ('admin', 'investigator')}

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
        extra = {}
        if custom:
            extra = dict(custom(actor, entity, data, lookup) or {})
        next_status = extra.pop("next_status", None) or next_status or entity["status"]
        patch = extra.pop("patch", None)
        if patch is None:
            patch = dict(data)
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
