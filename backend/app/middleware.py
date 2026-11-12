"""Outermost application middleware: security headers and the last-resort error handler.

This is plain ASGI (not ``@app.middleware``) so that it wraps the whole app, including errors
raised while a response is being generated or streamed:

- Every response gets the security headers, including the 500 produced here.
- An exception that nothing else handled is logged once (with a reference) and turned into the
  standard JSON error. The client never sees the exception message, SQL, parameters or a
  traceback.
- If the response had already started when the error happened, its status cannot be changed.
  The error is still logged once, and the body is deliberately left unfinished: the server
  then aborts the connection, so the client sees a failed transfer rather than a truncated
  body that looks complete. (uvicorn adds a one-line "returned without completing response"
  error; it carries no exception details.)

The exception is not re-raised: re-raising would make the server log the same traceback a
second time, without redaction guarantees.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import INTERNAL_ERROR_CODE, log_server_error, server_error_response

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}


# Never stored by the browser or any cache, errors included: personal, per-cookie cart and order
# responses, and PayPal webhook acknowledgements (M3.3).
NO_STORE_PREFIXES = ("/api/cart", "/api/webhooks")


def _no_store(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in NO_STORE_PREFIXES)


class SafetyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if _no_store(scope.get("path", "")):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as error:
            reference = log_server_error(scope, error, code=INTERNAL_ERROR_CODE)
            if not started:
                response = server_error_response(INTERNAL_ERROR_CODE, reference)
                await response(scope, receive, send_with_headers)
