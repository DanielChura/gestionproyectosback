from ..models import TaskStateDefinition


CATEGORIES = ("not_started", "active", "done", "closed")
LEGACY_CATEGORIES = {"PENDIENTE": "not_started", "COMPLETADA": "closed", "CANCELADA": "closed"}
DEFAULT_TEMA_IDS = {"not_started": 6, "active": 2, "done": 1, "closed": 1}
LEGACY_TEMA_IDS = {
    "PENDIENTE": 6, "EN_CURSO": 2, "COMPLETADA": 1,
    "CANCELADA": 3, "BLOQUEADA": 3, "EN_REVISION": 4, "OBSERVADA": 4,
}
COLORS = DEFAULT_TEMA_IDS
LEGACY_COLORS = LEGACY_TEMA_IDS


def definition_rules(project):
    return {
        item["code"]: "PENDIENTE" if item["category"] == "not_started" else
        "COMPLETADA" if item["category"] in ("done", "closed") else "EN_CURSO"
        for item in TaskStateDefinition.objects.filter(project=project).values("code", "category")
    }


def state_rule(project, code):
    return definition_rules(project).get(code, code)


def transition_policy(source, target, rules, legacy):
    if source == target:
        return None
    return {"supervision_required": False, "reason_required": False}
