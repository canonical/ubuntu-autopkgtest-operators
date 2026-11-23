"""Submit Tests.

Test all things related verifying input arguments and sending AMQP requests.
"""

import json
from time import time
from unittest import TestCase
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from helpers.exceptions import (
    BadRequest,
    WebControlException,
)

import request.submit


class SubmitTestBase(TestCase):
    """Common setup of tests of Submit class."""

    @patch(
        "request.submit.get_release_arches",
        return_value={"testy": ["6510", "C51"], "grumpy": ["hexium"]},
    )
    @patch("request.submit.KeyValueCache")
    @patch(
        "request.submit.get_autopkgtest_cloud_conf",
        MagicMock(
            return_value={
                "web": {"allowed_requestors": "list,of,groups,but,ignored"},
            }
        ),
    )
    def setUp(self, mock_cache, _mock_arches):
        mock_cache.return_value.get.return_value = None
        self.submit = request.submit.Submit()


class DistroRequestValidationTests(SubmitTestBase):
    """Test verification of distribution test requests."""

    def test_init(self):
        """Read available releases and architectures."""
        self.assertEqual(
            self.submit.release_arches,
            {"testy": ["6510", "C51"], "grumpy": ["hexium"]},
        )
        self.assertEqual(
            self.submit.allowed_teams, {"list", "of", "groups", "but", "ignored"}
        )

    def test_bad_release(self):
        """Unknown release."""
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request("fooly", "C51", "blue", ["ab/1"], "joe")
        self.assertEqual(str(cme.exception), "release fooly not found")

    def test_bad_arch(self):
        """Unknown architecture."""
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request("testy", "wut", "blue", ["ab/1"], "joe")
        self.assertEqual(str(cme.exception), "arch wut not found")

    def test_bad_argument(self):
        """Unknown argument."""
        with self.assertRaises(BadRequest) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1"], "joe", foo="bar"
            )
        self.assertIn("Invalid argument foo", str(cme.exception))

    def test_invalid_trigger_syntax(self):
        """Invalid syntax in trigger."""
        # invalid trigger format
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request("testy", "C51", "blue", ["ab"], "joe")
        self.assertIn("Malformed trigger", str(cme.exception))

        # invalid trigger source package name chars
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["a!b/1"], "joe"
            )

        # invalid trigger version chars
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1!1"], "joe"
            )
        self.assertIn("Malformed trigger", str(cme.exception))

    def test_disallowed_testname(self):
        """Testname not allowed for distro tests."""
        # we only allow this for GitHub requests; with distro requests it would
        # be cheating as proposed-migration would consider those
        with self.assertRaises(BadRequest) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1.2"], "joe", testname="first"
            )
        self.assertIn("Invalid argument testname", str(cme.exception))

    def test_ppa(self):
        """Reject malformed PPA names locally."""
        with self.assertRaises(BadRequest) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "foo", ["ab/1.2"], "joe", ["b~ad/ppa"]
            )
        self.assertEqual(str(cme.exception), "Malformed PPA name: b~ad/ppa")

    @patch("request.submit.Submit.lp_request", return_value={"entries": []})
    def test_nonmember_forbidden(self, _mock_lp_request):
        """Reject users outside the allowed teams."""
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1.2"], "joe"
            )
        self.assertEqual(cme.exception.exit_code(), 403)

    @patch(
        "request.submit.Submit.lp_request", return_value={"entries": [{"name": "list"}]}
    )
    def test_distro_all_proposed(self, _mock_lp_request):
        """Valid distro request with all-proposed is accepted."""
        self.submit.validate_distro_request(
            "testy", "C51", "blue", ["ab/1.2"], "joe", **{"all-proposed": "1"}
        )

    def test_distro_all_proposed_bad_value(self):
        """Valid distro request with invalid all-proposed value."""
        with self.assertRaises(BadRequest) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1.2"], "joe", **{"all-proposed": "bogus"}
            )
        self.assertIn("nvalid all-proposed value", str(cme.exception))

    @patch("request.submit.Submit.lp_request")
    def test_validate_distro_whitelisted_team(self, mock_lp_request):
        """A member of an allowed team can submit without publication lookups."""
        mock_lp_request.return_value = {"entries": [{"name": "list"}]}

        self.submit.validate_distro_request("testy", "C51", "blue", ["ab/1.2"], "joe")
        mock_lp_request.assert_called_once_with("~joe/super_teams", {"ws.size": "300"})

    @patch("request.submit.Submit.lp_request")
    def test_validate_distro_cached_user(self, mock_lp_request):
        """Recently allowed requester can submit without Launchpad lookups."""
        mock_lp_request.return_value = {"entries": [{"name": "list"}]}

        self.submit.allowed_user_cache.get.return_value = time()
        self.submit.validate_distro_request("testy", "C51", "blue", ["ab/1.2"], "joe")
        mock_lp_request.assert_not_called()

        self.submit.allowed_user_cache.get.return_value = (
            time() - request.submit.ALLOWED_USER_CACHE_TIME - 1
        )
        self.submit.validate_distro_request("testy", "C51", "blue", ["ab/1.2"], "joe")
        mock_lp_request.assert_called_once_with("~joe/super_teams", {"ws.size": "300"})

    def test_migration_reference_all_proposed_combo(self):
        """Tests when a test request has migration-reference/0 + all-proposed=1 in the request args."""
        with self.assertRaises(BadRequest) as cme:
            self.submit.validate_distro_request(
                "testy",
                "C51",
                "blue",
                ["migration-reference/0"],
                "joe",
                **{"all-proposed": "1"},
            )
        self.assertEqual(
            "migration-reference/0 and all-proposed=1 are not compatible arguments.",
            str(cme.exception),
        )


class GitRequestValidationTests(SubmitTestBase):
    """Test verification of git branch test requests."""

    def test_bad_release(self):
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "fooly", "C51", "ab", **{"build-git": "https://x.com/proj"}
            )
        self.assertEqual(str(cme.exception), "release fooly not found")

    def test_bad_arch(self):
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy", "wut", "a!b", **{"build-git": "https://x.com/proj"}
            )
        self.assertEqual(str(cme.exception), "arch wut not found")

    def test_bad_package(self):
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy", "C51", "a!b", **{"build-git": "https://x.com/proj"}
            )
        self.assertEqual(str(cme.exception), "package a!b not found")

    @patch("request.submit.urllib.request.urlopen")
    def test_unknown_ppa(self, mock_urlopen):
        # mock Launchpad response: PPA not found
        mock_urlopen.side_effect = HTTPError(
            "http://mock.launchpad.net", 404, "Not Found", {}, None
        )

        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy", "C51", "ab", ["bad/ppa"], **{"build-git": "https://x.com/proj"}
            )
        self.assertEqual(str(cme.exception), "ppa bad/ppa not found")
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_bad_env(self):
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy",
                "C51",
                "ab",
                env=["foo=1", "bar=1\n"],
                **{"build-git": "https://x.com/proj", "ppas": ["a/b"]},
            )
        self.assertIn("Invalid environment", str(cme.exception))
        self.assertIn("bar=1", str(cme.exception))

    def test_no_ppa(self):
        """No PPA."""
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy", "C51", "ab", **{"build-git": "https://x.com/proj"}
            )
        self.assertEqual(
            str(cme.exception),
            "Must specify at least one PPA (to associate results with)",
        )

    def test_bad_git_url(self):
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy",
                "C51",
                "ab",
                **{"build-git": "foo://x.com/proj", "ppas": ["a/b"]},
            )
        self.assertEqual(str(cme.exception), "Malformed build-git")

    def test_unknown_param(self):
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy",
                "C51",
                "ab",
                **{
                    "build-git": "http://x.com/proj",
                    "ppas": ["a/b"],
                    "foo": "bar",
                },
            )
        self.assertEqual(str(cme.exception), "Unsupported arguments: foo")

    def test_bad_testname(self):
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy",
                "C51",
                "ab",
                **{
                    "build-git": "http://x.com/proj",
                    "testname": "a !",
                    "ppas": ["a/b"],
                },
            )
        self.assertEqual(str(cme.exception), "Malformed testname")

    @patch("request.submit.Submit.lp_request", return_value={"name": "ppa"})
    def test_valid(self, _mock_lp_request):
        self.submit.validate_git_request(
            "testy",
            "C51",
            "ab",
            **{
                "build-git": "http://x.com/proj",
                "env": ["STATUS_URL=https://api.github.com/proj/123deadbeef"],
                "ppas": ["team/ppa"],
            },
        )

    @patch("request.submit.Submit.lp_request", return_value={"name": "ppa"})
    def test_branch(self, _mock_lp_request):
        self.submit.validate_git_request(
            "testy",
            "C51",
            "ab",
            **{
                "build-git": "http://x.com/proj#refs/pull/2/head",
                "env": ["STATUS_URL=https://api.github.com/proj/123deadbeef"],
                "ppas": ["team/ppa"],
            },
        )

    @patch("request.submit.Submit.lp_request", return_value={"name": "ppa"})
    def test_valid_testname(self, _mock_lp_request):
        self.submit.validate_git_request(
            "testy",
            "C51",
            "ab",
            **{
                "build-git": "http://x.com/proj",
                "testname": "first",
                "env": ["STATUS_URL=https://api.github.com/proj/123deadbeef"],
                "ppas": ["team/ppa"],
            },
        )


class SendAMQPTests(SubmitTestBase):
    """Test test request sending via AMQP."""

    @patch("request.submit.amqp_connect")
    def test_valid_request(self, mock_connect):
        self.submit.send_amqp_request(
            "testy",
            "C51",
            "foo",
            triggers=["ab/1"],
            requester="joe",
            ppas=["my/ppa"],
        )
        mock_connect.assert_called_once_with()
        cm_amqp_con = mock_connect.return_value.__enter__.return_value
        cm_channel = cm_amqp_con.channel.return_value.__enter__.return_value
        kwargs = cm_channel.basic_publish.call_args.kwargs
        self.assertEqual(kwargs["routing_key"], "debci-testy-C51")
        package, params = kwargs["body"].split("\n", 1)
        self.assertEqual(package, "foo")
        self.assertEqual(json.loads(params)["triggers"], ["ab/1"])
        self.assertEqual(json.loads(params)["ppas"], ["my/ppa"])
        self.assertEqual(json.loads(params)["requester"], "joe")
        self.assertIn("submit-time", json.loads(params))
