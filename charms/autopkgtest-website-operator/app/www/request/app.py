"""Test Request Flask App."""

import hmac
import json
import logging
import os
import pathlib
import sys
import traceback
from collections import ChainMap
from html import escape as _escape

from flask import Flask, redirect, request, session
from flask_openid import OpenID
from helpers.exceptions import WebControlException
from helpers.utils import get_github_context, setup_key
from request.submit import Submit
from werkzeug.middleware.proxy_fix import ProxyFix

# map multiple GET vars to AMQP JSON request parameter list
MULTI_ARGS = {"trigger": "triggers", "ppa": "ppas", "env": "env"}

EMPTY = ""

HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Autopkgtest Test Request</title>
</head>
<body>
{}
</body>
</html>
"""

LOGIN = """
<form action="/login" method="post">
<input type="submit" value="Log in with Ubuntu SSO">
<input type="hidden" name="next" value="{next}">
</form>
"""

LOGOUT = """
<p><a href="/logout">Logout {nickname}</a></p>
"""

ROW = """\
<dt>{}</dt>
<dd>{}</dd>
"""

SUCCESS = """
<p>Test request submitted.</p>
<dl>
{}
</dl>
"""


def check_github_sig(request):
    """Validate github signature of request.

    See https://developer.github.com/webhooks/securing/
    """
    # load key
    keyfile = os.path.expanduser("~/github-secrets.json")
    package = request.args.get("package")
    try:
        with open(keyfile) as f:
            keymap = json.load(f)
            key = keymap[package].encode("ASCII")
    except (IOError, ValueError, KeyError, UnicodeEncodeError) as e:
        logging.error("Failed to load GitHub key for package %s: %s", package, e)
        return False

    sig_sha1 = request.headers.get("X-Hub-Signature", "")
    payload_sha1 = "sha1=" + hmac.new(key, request.data, "sha1").hexdigest()
    if hmac.compare_digest(sig_sha1, payload_sha1):
        return True
    logging.error(
        "check_github_sig: signature mismatch! received: %s calculated: %s",
        sig_sha1,
        payload_sha1,
    )
    return False


def invalid(inv_exception, code=400):
    """Return message and HTTP error code for an invalid request and log it."""
    if "nickname" in session:
        html = LOGOUT.format(**session)
    else:
        html = ""
    message = str(inv_exception)
    if "\n" not in message:
        html += "<p>You submitted an invalid request: %s</p>" % maybe_escape(
            str(message)
        )
    else:
        html += "<p>You submitted an invalid request: </p>"
        list_of_messages = message.split("\n")
        for msg in list_of_messages:
            html += "<p>" + maybe_escape(str(msg)) + "</p>"
    logging.error("Request failed with %i: %s", code, message)
    return HTML.format(html), code


def maybe_escape(value):
    """Escape the value if it is True-ish."""
    return _escape(value) if value else value


def get_api_keys():
    """Get API keys.

    API keys is a json file like this:
    {
        "user1": "user1s-apikey",
        "user2": "user2s-apikey",
    }.
    """
    try:
        api_keys = json.loads(
            pathlib.Path("/home/ubuntu/external-web-requests-api-keys.json").read_text()
        )
    except Exception as e:
        logging.warning("Failed to read API keys: %s", e)
        api_keys = {}
    return api_keys


# Initialize app
PATH = os.path.join(
    os.path.sep, os.getenv("XDG_RUNTIME_DIR", "/run"), "autopkgtest_webcontrol"
)
os.makedirs(PATH, exist_ok=True)
app = Flask("request")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
# keep secret persistent between CGI invocations
secret_path = os.path.join(PATH, "secret_key")
setup_key(app, secret_path)
oid = OpenID(app, os.path.join(PATH, "openid"), safe_roots=[])


#
# Flask routes
#


@app.route("/", methods=["GET", "POST"])
def index_root():
    """Handle all GET requests."""
    session.permanent = True
    session["next"] = maybe_escape(request.url)
    nick = maybe_escape(session.get("nickname"))
    if "X-Api-Key" in request.cookies:
        key_user, api_key = request.cookies.get("X-Api-Key").split(":")
        request_creds_sha1 = hmac.new(
            key_user.encode(), api_key.encode(), "sha1"
        ).hexdigest()
        api_keys = get_api_keys()
        for user, user_key in api_keys.items():
            iter_creds_sha1 = hmac.new(
                user.encode(), user_key.encode(), "sha1"
            ).hexdigest()
            if hmac.compare_digest(request_creds_sha1, iter_creds_sha1):
                nick = key_user
                session.update(nickname=key_user)

    params = {maybe_escape(k): maybe_escape(v) for k, v in request.args.items()}
    # convert multiple GET args into lists
    for getarg, paramname in MULTI_ARGS.items():
        try:
            del params[getarg]
        except KeyError:
            pass
        l = request.args.getlist(getarg)
        if l:
            params[paramname] = [maybe_escape(p) for p in l]

    # split "VAR1=value;VAR2=value" --env arguments, as some frameworks don't
    # allow multiple "env="
    if "env" in params:
        splitenv = []
        for e in params["env"]:
            splitenv += e.split(";")
        params["env"] = splitenv

    # request from github?
    if b"api.github.com" in request.data:
        if not check_github_sig(request):
            return invalid("GitHub signature verification failed", 403)

        if request.headers.get("X-GitHub-Event") == "ping":
            return HTML.format("<p>OK</p>")

        github_params = request.get_json()
        if github_params.get("action") not in ["opened", "synchronize"]:
            return HTML.format(
                "<p>GitHub PR action %s is not relevant for testing</p>"
                % github_params.get("action", "<none>")
            )

        s = Submit()
        try:
            params.setdefault("env", []).append(
                "UPSTREAM_PULL_REQUEST=%i" % int(github_params["number"])
            )
            statuses_url = github_params["pull_request"]["statuses_url"]
            params["env"].append("GITHUB_STATUSES_URL=" + statuses_url)

            # support autopkgtests in upstream repos, set build-git URL to the
            # PR clone URL if not given
            if "build-git" not in params:
                params["build-git"] = "%s#refs/pull/%s/head" % (
                    github_params["pull_request"]["base"]["repo"]["clone_url"],
                    github_params["number"],
                )
            s.validate_git_request(**params)
        except WebControlException as e:
            return invalid(e, e.exit_code())
        except KeyError as e:
            return invalid("Missing field in JSON data: %s" % e)

        s.send_amqp_request(context="upstream", **params)
        # write status file for pending test
        os.makedirs(os.path.join(PATH, "github-pending"), exist_ok=True)
        with open(
            os.path.join(
                PATH,
                "github-pending",
                "%s-%s-%s-%s-%s-%s"
                % (
                    params["release"],
                    params["arch"],
                    params["package"],
                    params.get("testname", ""),
                    github_params["number"],
                    os.path.basename(statuses_url),
                ),
            ),
            "w",
        ) as f:
            f.write(json.dumps(params))

        # tell GitHub that the test is pending
        status = {
            "state": "pending",
            "context": get_github_context(params),
            "description": "autopkgtest running",
            "target_url": os.path.join(
                request.host_url, "running#pkg-" + params["package"]
            ),
        }
        s.post_json(
            statuses_url,
            status,
            os.path.expanduser("~/github-status-credentials.txt"),
            params["package"],
        )

        success = SUCCESS.format(
            EMPTY.join(ROW.format(key, val) for key, val in params.items())
        )
        return HTML.format(success)

    # distro request? Require SSO auth and validate_distro_request()
    elif nick:
        params["requester"] = nick
        s = Submit()
        if list(params.keys()) == ["/login", "requester"]:
            return redirect("/")
        try:
            s.validate_args(params)
            s.validate_distro_request(**params)
        except WebControlException as e:
            return invalid(e, e.exit_code())

        if params.get("delete"):
            del params["delete"]
            if params.get("ppas"):
                count = s.unsend_amqp_request(context="ppa", **params)
            else:
                count = s.unsend_amqp_request(**params)

            return HTML.format(
                LOGOUT + "<p>Deleted {} requests</p>".format(count)
            ).format(**ChainMap(session, params))

        if params.get("ppas"):
            uuid = s.send_amqp_request(context="ppa", **params)
        else:
            uuid = s.send_amqp_request(**params)
        # add link to result page for Ubuntu results
        if not params.get("ppas"):
            url = os.path.join(
                request.host_url,
                "packages",
                params["package"],
                params["release"],
                params["arch"],
            )
            params["Result history"] = '<a href="{}">{}</a>'.format(url, url)
            params["UUID"] = uuid
            params["Result url"] = os.path.join(
                request.host_url,
                "run",
                uuid,
            )
        success = SUCCESS.format(
            EMPTY.join(ROW.format(key, val) for key, val in sorted(params.items()))
        )
        return HTML.format(LOGOUT + success).format(**ChainMap(session, params))
    else:
        return HTML.format(LOGIN).format(**session), 403


@app.route("/login", methods=["GET", "POST"])
@oid.loginhandler
def login():
    """Initiate OpenID login."""
    if "nickname" in session:
        return redirect(oid.get_next_url())
    if "next" in request.form:
        return oid.try_login("https://login.ubuntu.com/", ask_for=["nickname"])
    return redirect("/")


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


@app.errorhandler(Exception)
def all_exception_handler(error):
    # If the exception doesn't have the exit_code method, it's not an expected
    # exception defined in helpers/exceptions.py
    try:
        return invalid(error, error.exit_code())
    except Exception:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        return (
            HTML.format(
                (
                    "<p>A server error has occurred. Traceback:</p> <pre>%s</pre>"
                    % "\n".join(
                        traceback.format_exception(exc_type, exc_value, exc_traceback)
                    )
                ),
            ),
            500,
        )
