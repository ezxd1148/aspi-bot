"""Admin-only dashboard, served on loopback inside the Telegram bot process."""

import asyncio
import base64
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
import jwt
from telegram.error import TelegramError

STATIC = Path(__file__).resolve().parents[1] / "dashboard"
SESSION_COOKIE = "__Host-aspi-session"
LOGIN_COOKIE = "__Host-aspi-login"
ISSUER = "https://oauth.telegram.org"


class LoginVerificationError(ValueError):
    """A fixed diagnostic code; never include provider response text or tokens."""


def _login_failure_reason(error):
    if isinstance(error, LoginVerificationError):
        return str(error)
    if isinstance(error, jwt.MissingRequiredClaimError):
        claim = error.claim if error.claim in ("iss", "aud", "exp", "iat", "sub", "id", "nonce") else "required"
        return "missing_claim_" + claim
    for kind, reason in ((jwt.ExpiredSignatureError, "token_expired"),
                         (jwt.ImmatureSignatureError, "token_not_yet_valid_check_server_clock"),
                         (jwt.InvalidAudienceError, "client_id_mismatch"),
                         (jwt.InvalidIssuerError, "issuer_mismatch"),
                         (jwt.InvalidSignatureError, "invalid_signature"),
                         (jwt.InvalidIssuedAtError, "invalid_issued_at"),
                         (jwt.PyJWTError, "invalid_id_token"),
                         (aiohttp.ContentTypeError, "unexpected_response_content_type"),
                         (aiohttp.ClientError, "telegram_connection_failed"),
                         (TimeoutError, "telegram_request_timed_out")):
        if isinstance(error, kind):
            return reason
    return "invalid_response"


@dataclass(frozen=True)
class Settings:
    origin: str
    client_id: str
    client_secret: str
    port: int = 8081

    @classmethod
    def from_environment(cls):
        origin = os.getenv("DASHBOARD_URL", "").strip().rstrip("/")
        if not origin:
            return None
        parsed = urlsplit(origin)
        try:
            parsed_port = parsed.port
        except ValueError:
            raise ValueError("DASHBOARD_URL contains an invalid port.") from None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or parsed_port == 0
                or any(character.isspace() for character in origin)):
            raise ValueError("DASHBOARD_URL must be an HTTPS origin, such as https://admin.example.com.")
        client_id = os.getenv("TELEGRAM_LOGIN_CLIENT_ID", "").strip()
        client_secret = os.getenv("TELEGRAM_LOGIN_CLIENT_SECRET", "").strip()
        if not client_id.isascii() or not client_id.isdigit() or int(client_id) <= 0 or not client_secret:
            raise ValueError("Configure TELEGRAM_LOGIN_CLIENT_ID and TELEGRAM_LOGIN_CLIENT_SECRET from BotFather.")
        port = int(os.getenv("DASHBOARD_PORT", "8081"))
        if not 1024 <= port <= 65535 or port == int(os.getenv("PORT", "8080")):
            raise ValueError("DASHBOARD_PORT must be 1024-65535 and different from the health-check PORT.")
        return cls(origin, client_id, client_secret, port)

    @property
    def callback_url(self):
        return self.origin + "/auth/callback"


class Dashboard:
    def __init__(self, settings, bot, review_chat_id, backend):
        self.settings = settings
        self.bot = bot
        self.review_chat_id = str(review_chat_id)
        self.backend = backend
        self.sessions = {}
        self.logins = {}
        self.login_attempts = deque()
        self.keys = []
        self.keys_until = 0
        self.http = None
        self.runner = None

    def _purge(self):
        now = time.time()
        self.logins = {k: v for k, v in self.logins.items() if v["expires"] > now}
        self.sessions = {k: v for k, v in self.sessions.items()
                         if v["expires"] > now and v["idle"] > now}

    def _limit_login(self):
        now = time.monotonic()
        while self.login_attempts and self.login_attempts[0] < now - 60:
            self.login_attempts.popleft()
        # Global bound also works behind a reverse proxy without trusting IP headers.
        if len(self.login_attempts) >= 30:
            raise web.HTTPTooManyRequests(text="Too many login attempts. Try again in a minute.")
        self.login_attempts.append(now)

    async def _role(self, user_id):
        if int(self.review_chat_id) > 0:
            return "Private admin" if user_id == int(self.review_chat_id) else None
        try:
            member = await asyncio.wait_for(self.bot.get_chat_member(
                chat_id=int(self.review_chat_id), user_id=user_id), timeout=10)
        except (TelegramError, TimeoutError):
            raise web.HTTPServiceUnavailable(text="Cannot verify Telegram admin access. Try again shortly.")
        if member.user.is_bot:
            return None
        return {"creator": "Owner", "administrator": "Administrator"}.get(member.status)

    @web.middleware
    async def security(self, request, handler):
        try:
            self._purge()
            public = request.path in ("/", "/assets/app.css", "/assets/app.js", "/auth/login", "/auth/callback")
            if not public:
                token = request.cookies.get(SESSION_COOKIE, "")
                session = self.sessions.get(token)
                if session is None:
                    raise web.HTTPUnauthorized(text="Sign in with Telegram to continue.")
                role = await self._role(session["user"].id)
                if role is None:
                    self.sessions.pop(token, None)
                    raise web.HTTPForbidden(text="Only current review-group administrators can enter.")
                now = time.time()
                requests = session["requests"]
                while requests and requests[0] < now - 60:
                    requests.popleft()
                if len(requests) >= 120:
                    raise web.HTTPTooManyRequests(text="Too many requests. Try again shortly.")
                requests.append(now)
                session["idle"] = now + 1800
                request["session"] = session
                request["role"] = role
                if request.method not in ("GET", "HEAD"):
                    if (request.headers.get("Origin") != self.settings.origin
                            or not hmac.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode())):
                        raise web.HTTPForbidden(text="Request verification failed. Refresh the dashboard.")
                    if request.content_type != "application/json":
                        raise web.HTTPUnsupportedMediaType(text="Expected JSON.")
            response = await handler(request)
        except web.HTTPException as error:
            if request.path == "/dashboard" and error.status in (401, 403):
                response = web.HTTPSeeOther(location="/")
                response.del_cookie(SESSION_COOKIE, path="/")
            elif request.path.startswith("/api/"):
                response = web.json_response({"error": error.text}, status=error.status)
            else:
                response = error
        except Exception as error:
            print(f"Dashboard request failed: {type(error).__name__}", flush=True)
            response = web.json_response({"error": "Request failed. Check the bot service logs."}, status=500)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Strict-Transport-Security": "max-age=31536000",
        })
        return response

    async def login_page(self, request):
        return web.Response(text=(STATIC / "login.html").read_text(), content_type="text/html")

    async def dashboard_page(self, request):
        return web.Response(text=(STATIC / "index.html").read_text(), content_type="text/html")

    async def asset(self, request):
        name = request.match_info["name"]
        if name not in ("app.js", "app.css"):
            raise web.HTTPNotFound()
        return web.Response(text=(STATIC / name).read_text(),
                            content_type="text/javascript" if name.endswith(".js") else "text/css")

    async def login(self, request):
        self._limit_login()
        if len(self.logins) >= 100:
            raise web.HTTPTooManyRequests(text="Try again shortly.")
        state, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.logins[state] = {"verifier": verifier, "nonce": nonce, "expires": time.time() + 300}
        query = urlencode({"client_id": self.settings.client_id, "redirect_uri": self.settings.callback_url,
                           "response_type": "code", "scope": "openid profile", "state": state,
                           "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"})
        response = web.HTTPSeeOther(location=ISSUER + "/auth?" + query)
        response.set_cookie(LOGIN_COOKIE, state, max_age=300, secure=True, httponly=True, samesite="Lax", path="/")
        return response

    async def _exchange(self, code, verifier):
        async with self.http.post(ISSUER + "/token", auth=aiohttp.BasicAuth(
                self.settings.client_id, self.settings.client_secret), data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": self.settings.callback_url, "client_id": self.settings.client_id,
                "code_verifier": verifier}, allow_redirects=False) as response:
            if response.status != 200:
                # Only allow known OAuth codes into logs, never response descriptions.
                reason = "token_endpoint_http_" + str(response.status)
                try:
                    data = await response.json()
                    oauth_error = data.get("error") if isinstance(data, dict) else None
                    if oauth_error in ("invalid_client", "invalid_grant", "invalid_request",
                                       "unauthorized_client", "unsupported_grant_type"):
                        reason += "_" + oauth_error
                except (ValueError, aiohttp.ClientError):
                    pass
                raise LoginVerificationError(reason)
            data = await response.json()
            token = data.get("id_token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise LoginVerificationError("token_endpoint_missing_id_token")
            return token

    async def _verify_token(self, token, nonce):
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise LoginVerificationError("unsupported_signing_algorithm_set_botfather_RS256")
        if not isinstance(header.get("kid"), str):
            raise LoginVerificationError("missing_signing_key_id")
        if time.time() > self.keys_until or not any(k.get("kid") == header["kid"] for k in self.keys):
            async with self.http.get(ISSUER + "/.well-known/jwks.json", allow_redirects=False) as response:
                if response.status != 200:
                    raise LoginVerificationError("signing_keys_http_" + str(response.status))
                data = await response.json()
                keys = data.get("keys") if isinstance(data, dict) else None
                if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
                    raise LoginVerificationError("invalid_signing_keys_response")
                self.keys = keys
                self.keys_until = time.time() + 3600
        key = next((k for k in self.keys if k.get("kid") == header["kid"]
                    and k.get("kty") == "RSA" and k.get("use", "sig") == "sig"), None)
        if key is None:
            raise LoginVerificationError("unknown_signing_key")
        claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm="RS256").key,
                            algorithms=["RS256"], issuer=ISSUER, audience=self.settings.client_id,
                            options={"require": ["iss", "aud", "exp", "iat", "sub", "id", "nonce"]})
        if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"].encode(), nonce.encode()):
            raise LoginVerificationError("nonce_mismatch")
        if time.time() - claims["iat"] > 300:
            raise LoginVerificationError("login_token_too_old")
        user_id = claims["id"]
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise LoginVerificationError("invalid_telegram_user_id")
        return SimpleNamespace(id=user_id, full_name=str(claims.get("name", "Telegram admin"))[:128],
                               username=str(claims.get("preferred_username", ""))[:64], is_bot=False)

    async def callback(self, request):
        self._limit_login()
        state = request.query.get("state", "")
        cookie = request.cookies.get(LOGIN_COOKIE, "")
        if not state or not cookie or not hmac.compare_digest(state.encode(), cookie.encode()):
            raise web.HTTPForbidden(text="Login verification failed. Start again from the login page.")
        login = self.logins.pop(state, None)
        if login is None:
            raise web.HTTPForbidden(text="Login expired or already used. Start again.")
        code = request.query.get("code", "")
        if not code or len(code) > 4096:
            raise web.HTTPForbidden(text="Telegram login was cancelled. Start again.")
        stage = "token_exchange"
        try:
            token = await self._exchange(code, login["verifier"])
            stage = "id_token_verification"
            user = await self._verify_token(token, login["nonce"])
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, jwt.PyJWTError) as error:
            print(f"Dashboard Telegram login failed: stage={stage} reason={_login_failure_reason(error)}", flush=True)
            raise web.HTTPForbidden(text="Could not verify Telegram login. Check the bot service logs, then start again.") from None
        if await self._role(user.id) is None:
            raise web.HTTPForbidden(text="Only administrators of the configured review group can enter.")
        if len(self.sessions) >= 100:
            raise web.HTTPTooManyRequests(text="Too many active sessions. Try again later.")
        now = time.time()
        session_token = secrets.token_urlsafe(32)
        old_token = request.cookies.get(SESSION_COOKIE, "")
        self.sessions.pop(old_token, None)
        self.sessions[session_token] = {"user": user, "csrf": secrets.token_urlsafe(32),
                                        "expires": now + 8 * 3600, "idle": now + 1800, "requests": deque()}
        response = web.HTTPSeeOther(location="/dashboard")
        response.set_cookie(SESSION_COOKIE, session_token, max_age=8 * 3600, secure=True,
                            httponly=True, samesite="Lax", path="/")
        response.del_cookie(LOGIN_COOKIE, path="/")
        return response

    async def session(self, request):
        session = request["session"]
        user = session["user"]
        return web.json_response({"user": {"id": user.id, "name": user.full_name,
                                           "username": user.username, "role": request["role"]},
                                  "csrf": session["csrf"]})

    async def logout(self, request):
        self.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def read(self, request):
        return web.json_response(await self.backend.read(request.match_info["section"], request.query))

    async def action(self, request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Invalid JSON.")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object.")
        return web.json_response(await self.backend.action(request.match_info["action"], body,
                                                         request["session"]["user"]))

    def application(self):
        app = web.Application(middlewares=[self.security], client_max_size=16384)
        app.add_routes([web.get("/", self.login_page), web.get("/dashboard", self.dashboard_page),
                        web.get("/assets/{name}", self.asset), web.get("/auth/login", self.login),
                        web.get("/auth/callback", self.callback), web.get("/api/session", self.session),
                        web.post("/api/logout", self.logout),
                        web.get("/api/{section:overview|reviews|activity|providers|admins}", self.read),
                        web.post("/api/{action:review|test-providers|reset}", self.action)])
        return app

    async def start(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.runner = web.AppRunner(self.application(), access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, "127.0.0.1", self.settings.port).start()
        except BaseException:
            await self.close()
            raise
        print(f"Dashboard listening on 127.0.0.1:{self.settings.port}; public URL {self.settings.origin}", flush=True)

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
        if self.http:
            await self.http.close()
        self.sessions.clear()
        self.logins.clear()
