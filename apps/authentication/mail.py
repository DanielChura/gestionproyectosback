import json
import smtplib
from email.utils import make_msgid
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import EmailMessage

from .tokens import digest_token


class DeliveryError(Exception):
    def __init__(self, message, delivery_status="failed"):
        super().__init__(message)
        self.delivery_status = delivery_status


def send_link(email, name, purpose, token):
    path = "activar" if purpose == "activation" else "restablecer"
    subject = "Activa tu cuenta GM" if purpose == "activation" else "Restablece tu contraseña GM"
    url = f"{settings.FRONTEND_URL}/{path}#token={token}"
    message = f"Hola {name},\n\nUsa este enlace para {'activar tu cuenta' if purpose == 'activation' else 'restablecer tu contraseña'}:\n{url}\n\nSi no solicitaste esto, ignora este mensaje."
    if settings.EMAIL_HOST:
        return send_smtp(email, subject, message)
    if not settings.RESEND_API_KEY or not settings.RESEND_FROM_EMAIL:
        raise DeliveryError("Servicio de correo no configurado")
    return send_resend(email, subject, message, token)


def send_smtp(email, subject, message):
    if not settings.MAIL_FROM_EMAIL:
        raise DeliveryError("Servicio de correo no configurado")
    message_id = make_msgid()
    mail = EmailMessage(
        subject,
        message,
        settings.MAIL_FROM_EMAIL,
        [email],
        headers={"Message-ID": message_id},
    )
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            mail.send(fail_silently=False)
            return message_id
        except (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPDataError,
        ):
            raise DeliveryError("El proveedor rechazó el envío") from None
        except (smtplib.SMTPException, OSError, TimeoutError):
            if attempt < max_attempts - 1:
                import time
                time.sleep(1)
                continue
            raise DeliveryError("No se confirmó la aceptación del correo", "unknown") from None
    return message_id


def send_resend(email, subject, message, token):
    payload = json.dumps(
        {"from": settings.RESEND_FROM_EMAIL, "to": [email], "subject": subject, "text": message}
    ).encode()
    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
            "Content-Type": "application/json",
            "Idempotency-Key": digest_token(token),
            "User-Agent": "gestionproyectos-backend/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if response.status not in (200, 201, 202):
                raise DeliveryError("No se pudo enviar el correo")
            try:
                result = json.loads(response.read())
            except (ValueError, UnicodeError):
                raise DeliveryError("No se confirmó la aceptación del correo", "unknown") from None
            message_id = result.get("id") if isinstance(result, dict) else None
            if not isinstance(message_id, str) or not message_id or len(message_id) > 128:
                raise DeliveryError("No se confirmó la aceptación del correo", "unknown")
            return message_id
    except HTTPError as error:
        delivery_status = "unknown" if error.code >= 500 else "failed"
        raise DeliveryError("El proveedor no confirmó el envío", delivery_status) from None
    except (URLError, TimeoutError, ConnectionError, HTTPException):
        raise DeliveryError("No se confirmó la aceptación del correo", "unknown") from None
