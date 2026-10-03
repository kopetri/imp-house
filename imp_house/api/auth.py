import time
from collections import OrderedDict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from imp_house.api.deps import (
    SESSION_COOKIE,
    AppState,
    current_user,
    get_session,
    get_state,
    require_csrf_header,
)
from imp_house.models import User
from imp_house.security import create_session_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[Depends(require_csrf_header)])


class LoginAttempts:
    def __init__(self, limit: int = 10, window_seconds: float = 15 * 60, max_keys: int = 4096) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str) -> deque[float]:
        entries = self._failures.get(key)
        if entries is None:
            if len(self._failures) >= self._max_keys:
                self._failures.popitem(last=False)
            entries = deque()
            self._failures[key] = entries
        else:
            self._failures.move_to_end(key)
        cutoff = time.monotonic() - self._window
        while entries and entries[0] < cutoff:
            entries.popleft()
        return entries

    def blocked(self, key: str) -> bool:
        return len(self._prune(key)) >= self._limit

    def fail(self, key: str) -> None:
        self._prune(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


login_attempts = LoginAttempts()


async def _lock_first_signup(session: AsyncSession) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(734811329)"))


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)


class UserOut(BaseModel):
    id: int
    email: str
    is_admin: bool
    is_active: bool

    model_config = {"from_attributes": True}


def _set_session_cookie(response: Response, state: AppState, user: User) -> None:
    token = create_session_token(user.id, state.settings.secret_key.get(), state.settings.session_days)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=state.settings.session_days * 86400,
        httponly=True,
        secure=state.settings.cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> User:
    await _lock_first_signup(session)
    is_first = not await session.scalar(select(func.count()).select_from(User))
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        is_admin=is_first,
        is_active=True,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from None
    _set_session_cookie(response, state, user)
    return user


@router.post("/login", response_model=UserOut)
async def login(
    body: Credentials,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> User:
    email = body.email.lower()
    keys = [f"ip:{request.client.host if request.client else '?'}", f"email:{email}"]
    if any(login_attempts.blocked(key) for key in keys):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts, try again later")
    user = await session.scalar(select(User).where(User.email == email))
    valid = verify_password(user.password_hash if user else None, body.password)
    if user is None or not valid or not user.is_active:
        for key in keys:
            login_attempts.fail(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    for key in keys:
        login_attempts.reset(key)
    _set_session_cookie(response, state, user)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> User:
    return user
