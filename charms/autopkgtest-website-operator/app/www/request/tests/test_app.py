"""Test the Flask app."""

import os
from unittest import TestCase
from unittest.mock import mock_open, patch

import request.app
from helpers.exceptions import WebControlException
from request.submit import Submit


class AppTestBase(TestCase):
    def setUp(self):
        request.app.app.config["TESTING"] = True
        self.app = request.app.app.test_client()


class DistroRequestTests(AppTestBase):
    """Test distribution test requests (via SSO)."""

    def prep_session(self):
        """Set some commonly needed session data."""
        with self.app.session_transaction() as session:
            session["nickname"] = "person"

    def test_login(self):
        """Hitting / when not logged in prompts for a login."""
        ret = self.app.get("/")
        self.assertIn(b'<form action="/login"', ret.data)

    def test_secret_key_persistence(self):
        """Secret key gets saved and loaded between app restarts."""
        orig_key = request.app.app.secret_key
        request.app.setup_key(request.app, request.app.secret_path)
        self.assertEqual(request.app.app.secret_key, orig_key)

    @patch("request.app.Submit")
    def test_nickname(self, mock_submit):
        """Hitting / with a nickname in the session prompts for logout."""
        mock_submit.return_value.validate_distro_request.side_effect = (
            WebControlException("not 31337 enough", 200)
        )
        with self.app.session_transaction() as session:
            session["nickname"] = "person"
        ret = self.app.get("/")
        self.assertIn(b"Logout person", ret.data)

    @patch("request.app.Submit")
    def test_missing_request(self, mock_submit):
        """Missing GET params should return 400."""
        mock_submit.return_value.validate_distro_request.side_effect = (
            WebControlException("not 31337 enough", 400)
        )
        self.prep_session()
        ret = self.app.get("/")
        self.assertEqual(ret.status_code, 400)
        self.assertIn(b"You submitted an invalid request", ret.data)

    @patch("request.app.Submit")
    def test_invalid_request(self, mock_submit):
        """Invalid GET params should return 400."""
        mock_submit.return_value.validate_distro_request.side_effect = (
            WebControlException("not 31337 enough", 400)
        )
        self.prep_session()
        ret = self.app.get("/?arch=i386&package=hi&release=testy&trigger=foo/1")
        self.assertEqual(ret.status_code, 400)
        self.assertIn(b"not 31337 enough", ret.data)
        mock_submit.return_value.validate_distro_request.assert_called_once_with(
            release="testy",
            arch="i386",
            package="hi",
            triggers=["foo/1"],
            requester="person",
        )

    @patch("request.app.Submit")
    def test_invalid_args(self, mock_submit):
        """Invalid GET params should return 400."""
        mock_submit.return_value.validate_args.side_effect = WebControlException(
            "not 31337 enough", 400
        )
        self.prep_session()
        ret = self.app.get("/?archi=i386&package=hi&release=testy&trigger=foo/1")
        self.assertEqual(ret.status_code, 400)
        self.assertIn(b"not 31337 enough", ret.data)
        mock_submit.return_value.validate_args.assert_called_once_with(
            {
                "archi": "i386",
                "package": "hi",
                "release": "testy",
                "triggers": ["foo/1"],
                "requester": "person",
            }
        )

    @patch("request.app.Submit")
    def test_valid_request(self, mock_submit):
        """Successful distro request with one trigger."""
        self.prep_session()
        ret = self.app.get("/?arch=i386&package=hi&release=testy&trigger=foo/1")
        self.assertEqual(ret.status_code, 200)
        self.assertIn(b"ubmitted", ret.data)
        mock_submit.return_value.validate_distro_request.assert_called_once_with(
            release="testy",
            arch="i386",
            package="hi",
            triggers=["foo/1"],
            requester="person",
        )

    @patch("request.app.Submit")
    def test_valid_request_multi_trigger(self, mock_submit):
        """Successful distro request with multiple triggers."""
        self.prep_session()
        ret = self.app.get(
            "/?arch=i386&package=hi&release=testy&trigger=foo/1&trigger=bar/2"
        )
        self.assertEqual(ret.status_code, 200)
        self.assertIn(b"ubmitted", ret.data)
        mock_submit.return_value.validate_distro_request.assert_called_once_with(
            release="testy",
            arch="i386",
            package="hi",
            triggers=["foo/1", "bar/2"],
            requester="person",
        )

    @patch("request.app.Submit")
    def test_valid_request_with_ppas(self, mock_submit):
        """Return success with all params & ppas."""
        self.prep_session()
        ret = self.app.get(
            "/?arch=i386&package=hi&release=testy&trigger=foo/1&ppa=train/overlay&ppa=train/001"
        )
        self.assertEqual(ret.status_code, 200)
        self.assertIn(b"ubmitted", ret.data)
        mock_submit.return_value.validate_distro_request.assert_called_once_with(
            release="testy",
            arch="i386",
            package="hi",
            triggers=["foo/1"],
            requester="person",
            ppas=["train/overlay", "train/001"],
        )
        mock_submit.return_value.send_amqp_request.assert_called_once_with(
            context="ppa",
            release="testy",
            arch="i386",
            package="hi",
            triggers=["foo/1"],
            requester="person",
            ppas=["train/overlay", "train/001"],
        )

    @patch("request.app.Submit")
    def test_all_proposed(self, mock_submit):
        """Successful distro request with all-proposed."""
        self.prep_session()
        ret = self.app.get(
            "/?arch=i386&package=hi&release=testy&trigger=foo/1&all-proposed=1"
        )
        self.assertEqual(ret.status_code, 200)
        self.assertIn(b"ubmitted", ret.data)
        mock_submit.return_value.validate_distro_request.assert_called_once_with(
            release="testy",
            arch="i386",
            package="hi",
            triggers=["foo/1"],
            requester="person",
            **{"all-proposed": "1"},
        )
        mock_submit.return_value.send_amqp_request.assert_called_once_with(
            release="testy",
            arch="i386",
            package="hi",
            triggers=["foo/1"],
            requester="person",
            **{"all-proposed": "1"},
        )


class GitHubRequestTests(AppTestBase):
    """Test GitHub test requests (via PSK signatures)."""

    @patch(
        "request.app.open",
        mock_open(None, '{"hi": "1111111111111111111111111111111111111111"}'),
        create=True,
    )
    def test_ping(self):
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo",
            content_type="application/json",
            headers=[
                (
                    "X-Hub-Signature",
                    "sha1=cb59904bf33c619ad2c52095deb405c86cc5adfd",
                ),
                ("X-GitHub-Event", "ping"),
            ],
            data=b'{"info": "https://api.github.com/xx"}',
        )
        self.assertEqual(ret.status_code, 200, ret.data)
        self.assertIn(b"OK", ret.data)
        self.assertNotIn(b"ubmit", ret.data)

    @patch("request.app.Submit")
    @patch("request.app.open", mock_open(None, "bogus"), create=True)
    def test_invalid_secret_file(self, mock_submit):
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo",
            content_type="application/json",
            headers=[
                (
                    "X-Hub-Signature",
                    "sha1=8572f239e05c652710a4f85d2061cc0fcbc7b127",
                )
            ],
            data=b'{"action": "opened", "number": 2, "pr": "https://api.github.com/xx"}',
        )

        self.assertEqual(ret.status_code, 403, ret.data)
        self.assertIn(b"GitHub signature verification failed", ret.data)
        self.assertFalse(mock_submit.return_value.validate_git_request.called)
        self.assertFalse(mock_submit.return_value.send_amqp_request.called)

    @patch(
        "request.app.open",
        mock_open(None, '{"hi": "1111111111111111111111111111111111111111"}'),
        create=True,
    )
    def test_bad_signature(self):
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo",
            content_type="application/json",
            headers=[
                ("X-Hub-Signature", "sha1=deadbeef0815"),
                ("X-GitHub-Event", "ping"),
            ],
            data=b'{"info": "https://api.github.com/xx"}',
        )
        self.assertEqual(ret.status_code, 403, ret.data)
        self.assertIn(b"GitHub signature verification failed", ret.data)

    @patch("request.app.Submit")
    @patch("request.app.check_github_sig")
    def test_missing_pr_number(self, mock_check_github_sig, mock_submit):
        mock_check_github_sig.return_value = True
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo",
            content_type="application/json",
            data=b'{"action": "opened", "pr": "https://api.github.com/xx"}',
        )
        self.assertEqual(ret.status_code, 400, ret.data)
        self.assertIn(b"Missing field in JSON data: &#x27;number&#x27;", ret.data)
        self.assertFalse(mock_submit.return_value.validate_git_request.called)
        self.assertFalse(mock_submit.return_value.send_amqp_request.called)

    @patch("request.app.Submit")
    @patch("request.app.check_github_sig")
    def test_ignored_action(self, mock_check_github_sig, mock_submit):
        mock_check_github_sig.return_value = True
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo",
            content_type="application/json",
            data=b'{"action": "boring", "number": 2, "pr": "https://api.github.com/xx"}',
        )
        self.assertEqual(ret.status_code, 200, ret.data)
        self.assertIn(b"GitHub PR action boring is not relevant for testing", ret.data)
        self.assertFalse(mock_submit.return_value.validate_git_request.called)
        self.assertFalse(mock_submit.return_value.send_amqp_request.called)

    @patch("request.app.Submit")
    @patch("request.app.check_github_sig")
    def test_invalid(self, mock_check_github_sig, mock_submit):
        mock_submit.return_value.validate_git_request.side_effect = WebControlException(
            "weird color", 400
        )
        mock_check_github_sig.return_value = True
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo",
            content_type="application/json",
            data=b'{"action": "opened", "number": 2, "pull_request":'
            b'{"statuses_url": "https://api.github.com/2"}}',
        )
        self.assertEqual(ret.status_code, 400, ret.data)
        self.assertIn(b"invalid request", ret.data)
        self.assertIn(b"weird color", ret.data)
        mock_submit.return_value.validate_git_request.assert_called_once_with(
            release="testy",
            arch="C51",
            package="hi",
            env=[
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/2",
            ],
            **{"build-git": "http://x.com/foo"},
        )
        self.assertFalse(mock_submit.return_value.send_amqp_request.called)

    @patch("request.app.Submit")
    @patch(
        "request.app.open",
        mock_open(None, '{"hi": "1111111111111111111111111111111111111111"}'),
        create=True,
    )
    def test_valid_simple(self, mock_submit):
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo",
            content_type="application/json",
            headers=[
                (
                    "X-Hub-Signature",
                    "sha1=1dae67d4406d21b498806968a3def61754498a21",
                )
            ],
            data=b'{"action": "opened", "number": 2, "pull_request":'
            b' {"statuses_url": "https://api.github.com/two"}}',
        )

        self.assertEqual(ret.status_code, 200, ret.data)
        self.assertIn(b"Test request submitted.", ret.data)
        mock_submit.return_value.validate_git_request.assert_called_once_with(
            release="testy",
            arch="C51",
            package="hi",
            env=[
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/two",
            ],
            **{"build-git": "http://x.com/foo"},
        )
        mock_submit.return_value.send_amqp_request.assert_called_once_with(
            context="upstream",
            release="testy",
            arch="C51",
            package="hi",
            env=[
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/two",
            ],
            **{"build-git": "http://x.com/foo"},
        )

        # we recorded the request
        request.app.open.assert_called_with(
            os.path.join(request.app.PATH, "github-pending", "testy-C51-hi--2-two"),
            "w",
        )
        self.assertIn(
            "GITHUB_STATUSES_URL=https://api.github.com/two",
            str(request.app.open().write.call_args),
        )
        self.assertIn('"arch": "C51"', str(request.app.open().write.call_args))

        # we told GitHub about it
        mock_submit.return_value.post_json.assert_called_once_with(
            "https://api.github.com/two",
            {
                "context": "testy/C51",
                "state": "pending",
                "target_url": "http://localhost/running#pkg-hi",
                "description": "autopkgtest running",
            },
            os.path.expanduser("~/github-status-credentials.txt"),
            "hi",
        )

    @patch("request.app.Submit")
    @patch(
        "request.app.open",
        mock_open(None, '{"hi": "1111111111111111111111111111111111111111"}'),
        create=True,
    )
    def test_valid_complex(self, mock_submit):
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo&"
            "ppa=joe/stuff&ppa=mary/misc&env=THIS=a;THAT=b&env=THERE=c&"
            "testname=integration",
            content_type="application/json",
            headers=[
                (
                    "X-Hub-Signature",
                    "sha1=f9041325575127310c304bb65f9befb0d13b1ce6",
                )
            ],
            data=b'{"action": "opened", "number": 2, "pull_request":'
            b' {"statuses_url": "https://api.github.com/2"}}',
        )

        self.assertEqual(ret.status_code, 200, ret.data)
        self.assertIn(b"Test request submitted.", ret.data)
        mock_submit.return_value.validate_git_request.assert_called_once_with(
            release="testy",
            arch="C51",
            package="hi",
            env=[
                "THIS=a",
                "THAT=b",
                "THERE=c",
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/2",
            ],
            ppas=["joe/stuff", "mary/misc"],
            **{"build-git": "http://x.com/foo", "testname": "integration"},
        )
        mock_submit.return_value.send_amqp_request.assert_called_once_with(
            context="upstream",
            release="testy",
            arch="C51",
            package="hi",
            env=[
                "THIS=a",
                "THAT=b",
                "THERE=c",
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/2",
            ],
            ppas=["joe/stuff", "mary/misc"],
            **{"build-git": "http://x.com/foo", "testname": "integration"},
        )
        mock_submit.return_value.post_json.assert_called_once_with(
            "https://api.github.com/2",
            {
                "context": "testy/C51 integration",
                "state": "pending",
                "target_url": "http://localhost/running#pkg-hi",
                "description": "autopkgtest running",
            },
            os.path.expanduser("~/github-status-credentials.txt"),
            "hi",
        )

    @patch("request.app.Submit")
    @patch(
        "request.app.open",
        mock_open(None, '{"hi": "1111111111111111111111111111111111111111"}'),
        create=True,
    )
    def test_valid_generated_url(self, mock_submit):
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy",
            content_type="application/json",
            headers=[
                (
                    "X-Hub-Signature",
                    "sha1=427a20827d46f5fe8e18f08b9a7fa09ba915ea08",
                )
            ],
            data=b'{"action": "opened", "number": 2, "pull_request":'
            b' {"statuses_url": "https://api.github.com/two",'
            b'  "base": {"repo": {"clone_url": "https://github.com/joe/x.git"}}}}',
        )

        self.assertEqual(ret.status_code, 200, ret.data)
        self.assertIn(b"Test request submitted.", ret.data)
        mock_submit.return_value.validate_git_request.assert_called_once_with(
            release="testy",
            arch="C51",
            package="hi",
            env=[
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/two",
            ],
            **{"build-git": "https://github.com/joe/x.git#refs/pull/2/head"},
        )
        mock_submit.return_value.send_amqp_request.assert_called_once_with(
            context="upstream",
            release="testy",
            arch="C51",
            package="hi",
            env=[
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/two",
            ],
            **{"build-git": "https://github.com/joe/x.git#refs/pull/2/head"},
        )

    def test_post_json_missing_file(self):
        self.assertRaises(
            IOError,
            Submit.post_json,
            "https://foo",
            {},
            "/non/existing",
            "myproj",
        )

    @patch(
        "request.submit.open",
        mock_open(None, "proj1:user:s3kr1t"),
        create=True,
    )
    @patch("request.submit.urllib.request")
    def test_post_json_nouser(self, mock_request):
        Submit.post_json("https://example.com", {"bar": 2}, "/the/creds.txt", "proj")
        self.assertEqual(mock_request.urlopen.call_count, 0)

    # this can only be tested shallowly in a unit test, this would need a real
    # web server
    @patch("request.submit.open", mock_open(None, "proj:user:s3kr1t"), create=True)
    @patch("request.submit.urllib.request")
    def test_post_json_success(self, mock_request):
        Submit.post_json("https://example.com", {"bar": 2}, "/the/creds.txt", "proj")
        print(mock_request.mock_calls)
        mock_request.Request.assert_called_once_with(
            url="https://example.com",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic dXNlcjpzM2tyMXQ=",
            },
            method="POST",
            data=b'{"bar": 2}',
        )
        self.assertEqual(mock_request.urlopen.call_count, 1)

    @patch("request.app.Submit")
    @patch(
        "request.app.open",
        mock_open(None, '{"hi": "1111111111111111111111111111111111111111"}'),
        create=True,
    )
    def test_valid_testname(self, mock_submit):
        ret = self.app.post(
            "/?arch=C51&package=hi&release=testy&build-git=http://x.com/foo&testname=first",
            content_type="application/json",
            headers=[
                (
                    "X-Hub-Signature",
                    "sha1=1dae67d4406d21b498806968a3def61754498a21",
                )
            ],
            data=b'{"action": "opened", "number": 2, "pull_request":'
            b' {"statuses_url": "https://api.github.com/two"}}',
        )

        self.assertEqual(ret.status_code, 200, ret.data)
        self.assertIn(b"Test request submitted.", ret.data)
        mock_submit.return_value.validate_git_request.assert_called_once_with(
            release="testy",
            arch="C51",
            package="hi",
            testname="first",
            env=[
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/two",
            ],
            **{"build-git": "http://x.com/foo"},
        )
        mock_submit.return_value.send_amqp_request.assert_called_once_with(
            context="upstream",
            release="testy",
            arch="C51",
            package="hi",
            testname="first",
            env=[
                "UPSTREAM_PULL_REQUEST=2",
                "GITHUB_STATUSES_URL=https://api.github.com/two",
            ],
            **{"build-git": "http://x.com/foo"},
        )

        # we recorded the request
        request.app.open.assert_called_with(
            os.path.join(
                request.app.PATH, "github-pending", "testy-C51-hi-first-2-two"
            ),
            "w",
        )
        self.assertIn(
            "GITHUB_STATUSES_URL=https://api.github.com/two",
            str(request.app.open().write.call_args),
        )
        self.assertIn('"testname": "first"', str(request.app.open().write.call_args))


SESSION = {}


class LoginTests(AppTestBase):
    """Test OpenID Logins."""

    def test_login(self):
        """Ensure correct redirect when initiating login."""
        ret = self.app.post(
            "/login",
            data=dict(
                openid="test",
                next="/",
            ),
            follow_redirects=False,
        )
        self.assertIn(b"https://login.ubuntu.com/+openid?", ret.data)
        self.assertEqual(ret.status_code, 302)

    def test_login_get(self):
        """Ensure login endpoint accepts GET requests as per SSO spec."""
        ret = self.app.get("/login", follow_redirects=False)
        self.assertIn(b'<a href="/">/</a>.', ret.data)
        self.assertEqual(ret.status_code, 302)

    def test_logged_already(self):
        """Ensure correct redirect when already logged in."""
        with self.app.session_transaction() as session:
            session["nickname"] = "person"
        ret = self.app.get("/login", follow_redirects=False)
        self.assertIn(b"You should be redirected automatically", ret.data)
        self.assertEqual(ret.status_code, 302)

    @patch("request.app.oid")
    @patch("request.app.session", SESSION)
    def test_identify(self, oid_mock):
        """Ensure OpenID login can be successfully completed."""

        class Resp:
            """Fake OpenID response class."""

            identity_url = "http://example.com"
            nickname = "lebowski"

        oid_mock.get_next_url.return_value = "https://localhost/"
        ret = request.app.identify(Resp)
        self.assertIn(b">https://localhost/</a>", ret.data)
        for attr in ("identity_url", "nickname"):
            self.assertEqual(getattr(Resp, attr), SESSION[attr])
        oid_mock.get_next_url.assert_called_once_with()
        self.assertEqual(ret.status_code, 302)

    def test_logout(self):
        """Ensure logging out correctly clears session."""
        with self.app.session_transaction() as session:
            session["foo"] = "bar"
        ret = self.app.get("/logout", follow_redirects=False)
        self.assertIn(b"http://localhost/</a>.", ret.data)
        self.assertEqual(ret.status_code, 302)
        with self.app.session_transaction() as session:
            self.assertNotIn("foo", session)
