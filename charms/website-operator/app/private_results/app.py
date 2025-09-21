"""Test Result Fetcher Flask App"""

import logging
import os
import sys
from html import escape

import swiftclient
from flask import (
    Flask,
    Response,
    redirect,
    render_template_string,
    request,
    session,
)
from flask_openid import OpenID
from helpers.utils import setup_key, swift_connect
from request.submit import Submit
from werkzeug.middleware.proxy_fix import ProxyFix

sys.path.append("..")

HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Autopkgtest Private Test Result Fetcher</title>
</head>
<body>
{{ content }}
</body>
</html>
"""

LOGIN = """
<form action="/private-results/login" method="post">
<input type="submit" value="Log in with Ubuntu SSO">
<input type="hidden" name="next" value="{{ next }}">
</form>
"""

DENIED_ACC = "Unprivileged! You can't access these logs."

DENIED_OBJ = (
    "Denied! The result couldn't be acquired. Please speak "
    + "to a member of the Canonical Ubuntu QA team."
)


def swift_get_object(connection, container, path):
    """Fetch an object from swift."""
    try:
        _, contents = connection.get_object(container, path)
    except swiftclient.exceptions.ClientException as e:
        logging.error("Failed to fetch %s from container (%s)" % (path, str(e)))
        return None
    return contents


def validate_user_path(connection, container, nick, path):
    """Return true if user is allowed to view files under the given path."""
    # First we need to check if this result is actually sharable
    allowed_file = swift_get_object(connection, container, path)
    if not allowed_file:
        return False
    allowed = allowed_file.decode("utf-8").splitlines()
    # Check if user is allowed
    # (separate step not to do unnecessary LP API calls)
    if nick in allowed:
        return True
    # Check if user is allowed via team membership
    for entity in allowed:
        (code, response) = Submit.lp_request("~%s/participants" % entity, {})
        if code != 200:
            logging.error("Unable to validate user %s (%s)" % (nick, code))
            return False
        for e in response.get("entries", []):
            if e.get("name") == nick:
                return True
    return False


# Initialize app
PATH = os.path.join(os.getenv("TMPDIR", "/tmp"), "autopkgtest_webcontrol")
os.makedirs(PATH, exist_ok=True)
app = Flask("private-results")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
# Keep secret persistent between CGI invocations
secret_path = os.path.join(PATH, "secret_key")
setup_key(app, secret_path)
oid = OpenID(app, os.path.join(PATH, "openid"), safe_roots=[])
# Connect to swift
connection = swift_connect()


#
# Flask routes
#


@app.route("/", methods=["GET"])
def index_root():
    """Handle the main index root, just pure informational."""
    return render_template_string(
        HTML, content="Please provide the path to the private result."
    )


@app.route(
    "/<container>/<series>/<arch>/<group>/<src>/<runid>/<file>",
    methods=["GET"],
)
def index_result(container, series, arch, group, src, runid, file):
    """Handle all GET requests for private tests."""
    session.permanent = True
    session["next"] = escape(request.url)
    if not container.startswith("private-"):
        return render_template_string(HTML, content="Limited to private results only.")
    nick = session.get("nickname")
    if nick:
        # Authenticated via SSO, so that's a start
        parent_path = os.path.join(series, arch, group, src, runid)
        object_path = os.path.join(parent_path, file)
        acl_path = os.path.join(parent_path, "readable-by")
        if not validate_user_path(connection, container, nick, acl_path):
            return render_template_string(HTML, content=DENIED_ACC), 403
        # We can pull the result now
        result = swift_get_object(connection, container, object_path)
        if result is None:
            return render_template_string(HTML, content=DENIED_OBJ), 404
        if file.endswith(".gz"):
            content_type = "text/plain; charset=UTF-8"
            headers = {"Content-Encoding": "gzip"}
            return Response(result, content_type=content_type, headers=headers)
        else:
            return result
    else:
        # XXX: render_template_string urlencodes its context values, so it's
        #  not really possible to have 'nested HTML' rendered properly.
        return HTML.replace("{{ content }}", render_template_string(LOGIN, **session))


@app.route("/login", methods=["GET", "POST"])
@oid.loginhandler
def login():
    """Initiate OpenID login."""
    if "nickname" in session:
        return redirect(oid.get_next_url())
    if "next" in request.form:
        return oid.try_login("https://login.ubuntu.com/", ask_for=["nickname"])
    return redirect("/private-results")


@oid.after_login
def identify(resp):
    """Complete OpenID login."""
    session.update(
        identity_url=resp.identity_url,
        nickname=resp.nickname,
    )
    return redirect(oid.get_next_url())


@app.route("/logout")
def logout():
    """Clear user session, logging them out."""
    session.clear()
    return redirect(oid.get_next_url())
