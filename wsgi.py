"""Production entry point, used by gunicorn on the server.

run.bat still starts app.py directly, so nothing here changes how the tool
behaves on the laptop. Three things only matter once it is reachable from the
internet:

1. The schema has to exist. app.py only calls db.init() under __main__, which
   gunicorn never runs.
2. The session cookie must be marked secure, so the browser refuses to send it
   over plain http. Tied to PUBLIC_URL rather than hardcoded, so it switches
   itself on the moment the certificate is in place.
3. nginx terminates TLS, so the app has to be told to trust X-Forwarded-Proto.
   Without it every request looks like http from the inside.
"""
from werkzeug.middleware.proxy_fix import ProxyFix

from app import app
from mailer import config, db

db.init()

app.config.update(
    SESSION_COOKIE_SECURE=config.PUBLIC_URL.startswith("https://"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PREFERRED_URL_SCHEME="https",
)

# Exactly one proxy in front of this: nginx, on this same machine. Trusting a
# larger number would let a caller forge its own client address.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
