import logging

logger = logging.getLogger("apps.authentication.audit")


def record_auth_event(action, *, actor_id=None, account_id=None, detail=""):
    logger.info(
        "auth_event action=%s actor_id=%s account_id=%s detail=%s",
        action,
        actor_id,
        account_id,
        detail,
    )
