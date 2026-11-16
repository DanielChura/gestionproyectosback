from django.core.exceptions import ValidationError

GMAIL_DOMAIN = "gmail.com"
GMAIL_MESSAGE = (
    "El correo debe ser una cuenta de Gmail terminada en @gmail.com. "
    "Corrige el correo e intenta de nuevo."
)


def gmail_address(value):
    email = str(value or "").strip().lower()
    local, separator, domain = email.partition("@")
    if not local or separator != "@" or domain != GMAIL_DOMAIN:
        raise ValidationError(GMAIL_MESSAGE)
    return email
