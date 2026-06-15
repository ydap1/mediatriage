from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from fastapi import Request
from fastapi.responses import RedirectResponse

from .config import settings

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="session")

COOKIE_NAME = "ft_session"
PUBLIC_PATHS = {"/login", "/logout"}


def make_session_cookie(response, value: str = "authenticated") -> None:
    token = _serializer.dumps(value)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME)


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=settings.session_max_age)
        return True
    except (BadSignature, SignatureExpired):
        return False


def require_auth(request: Request):
    if request.url.path in PUBLIC_PATHS:
        return None
    if not is_authenticated(request):
        raise _AuthRedirect()


class _AuthRedirect(Exception):
    pass
