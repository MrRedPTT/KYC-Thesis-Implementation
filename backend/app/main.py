import json
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

_HERE = Path(__file__).resolve()
for _candidate in (_HERE.parents[1] / ".env", _HERE.parents[2] / ".env"):
    if _candidate.is_file():
        load_dotenv(_candidate)
        break

from app.routers import onboarding, verification, identity  # noqa: E402

# ---------------------------------------------------------------------------
# Rate limiting — sliding window, per IP, no external dependencies
# ---------------------------------------------------------------------------

_hits: dict[str, list[float]] = defaultdict(list)

_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

# (max_requests, window_seconds)
_LIMITS: list[tuple[str, int, int]] = [
    ("/api/v1/onboarding/start",     5,  60),  # session creation
    ("/biometric",                   5,  60),  # expensive AI step
    ("/api/v1/onboarding",          30,  60),  # all onboarding calls
]
_DEFAULT_LIMIT = (60, 60)


def _get_limit(path: str) -> tuple[int, int]:
    for prefix, limit, window in _LIMITS:
        if prefix in path:
            return limit, window
    return _DEFAULT_LIMIT


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        ip    = request.client.host if request.client else "127.0.0.1"
        now   = time.monotonic()
        limit, window = _get_limit(request.url.path)
        key   = f"{ip}:{request.url.path}"

        # Evict timestamps outside the sliding window
        _hits[key] = [t for t in _hits[key] if now - t < window]

        if len(_hits[key]) >= limit:
            retry_after = int(window - (now - _hits[key][0])) + 1
            return Response(
                content=json.dumps({"detail": "Rate limit exceeded. Try again later."}),
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "Content-Type": "application/json",
                },
            )

        _hits[key].append(now)
        return await call_next(request)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="KYC Blockchain Backend",
    description="AI-powered KYC system with blockchain-based DID/VC identity management",
    version="1.0.0",
)

app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(onboarding.router,   prefix="/api/v1/onboarding",   tags=["Onboarding"])
app.include_router(verification.router, prefix="/api/v1/verification",  tags=["Verification"])
app.include_router(identity.router,     prefix="/api/v1/identity",      tags=["Identity"])


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok"}
