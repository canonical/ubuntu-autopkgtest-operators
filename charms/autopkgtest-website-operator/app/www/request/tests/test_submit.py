"""Submit Tests.

Test all things related verifying input arguments and sending AMQP requests.
"""

import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

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

    @patch("request.submit.urllib.request.urlopen")
    def test_bad_package(self, mock_urlopen):
        """Unknown package."""
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 200
        cm.geturl.return_value = "http://mock.launchpad.net"
        cm.read.side_effect = [
            b'{"entries": [{"component_name": "main"}]}',
            b'{"entries": []}',
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "badpkg", ["ab/1"], "joe"
            )
        self.assertEqual(str(cme.exception), "badpkg is not published in testy")

    def test_bad_argument(self):
        """Unknown argument."""
        with self.assertRaises(BadRequest) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1"], "joe", foo="bar"
            )
        self.assertIn("Invalid argument foo", str(cme.exception))

    @patch("request.submit.urllib.request.urlopen")
    def test_invalid_trigger_syntax(self, mock_urlopen):
        """Invalid syntax in trigger."""
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 200
        cm.geturl.return_value = "http://mock.launchpad.net"
        cm.read.side_effect = [
            b'{"entries": []}',
            b'{"entries": []}',
            b'{"entries": []}',
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

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

    @patch("request.submit.urllib.request.urlopen")
    def test_ppa(self, mock_urlopen):
        """PPA does not exist."""
        # invalid name don't even call lp
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "foo", ["ab/1.2"], "joe", ["b~ad/ppa"]
            )
        self.assertEqual(str(cme.exception), "ppa b~ad/ppa not found")
        self.assertEqual(mock_urlopen.call_count, 0)

        # mock Launchpad response: missing PPA, then a valid PPA
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 404
        cm.geturl.return_value = "http://mock.launchpad.net"
        cm.read.side_effect = [b"{}", b'{"name": "there"}', b"not { json}", b"{}"]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "foo", ["ab/1.2"], "joe", ["bad/ppa"]
            )
        self.assertEqual(str(cme.exception), "ppa bad/ppa not found")
        self.assertEqual(mock_urlopen.call_count, 1)

        # success
        cm.getcode.return_value = 200
        self.assertTrue(self.submit.is_valid_ppa("hi/there"))

        # broken JSON response
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "foo", ["ab/1.2"], "joe", ["broke/ness"]
            )

        # same, but entirely failing query -- let's be on the safe side
        cm.getcode.return_value = 404
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "foo", ["ab/1.2"], "joe", ["bro/ken"]
            )
        self.assertEqual(str(cme.exception), "ppa bro/ken not found")

    @patch("request.submit.urllib.request.urlopen")
    def test_nonexisting_trigger(self, mock_urlopen):
        """Trigger source package/version does not exist."""
        # mock Launchpad response: successful form, but no matching
        # source/version
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 200
        cm.geturl.return_value = "http://mock.launchpad.net"
        cm.read.side_effect = [
            b'{"entries": []}',
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1.2"], "joe"
            )
        self.assertEqual(str(cme.exception), "ab/1.2 is not published in testy")
        self.assertEqual(mock_urlopen.call_count, 1)

        # broken JSON response
        self.submit.lp_cache.clear()
        cm.read.side_effect = [b"not { json}"]
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1.2"], "joe"
            )

        # same, but entirely failing query -- let's be on the safe side
        self.submit.lp_cache.clear()
        cm.getcode.return_value = 404
        cm.read.side_effect = [b"{}"]
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1.2"], "joe"
            )
        self.assertEqual(cme.exception.exit_code(), 503)

    @patch("request.submit.urllib.request.urlopen")
    def test_bad_package_ppa(self, mock_urlopen):
        """Unknown package with a PPA request, assert no exception."""
        # mock Launchpad response: successful form, but no matching
        # source/version
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 200
        cm.geturl.return_value = "http://mock.launchpad.net"
        cm.read.side_effect = [
            b'{"name": "overlay"}',
            b'{"name": "goodstuff"}',
            b'{"entries": [{"component_name": "main"}]}',
            b'{"entries": [{"component_name": "main"}]}',
            b"true",
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        self.submit.validate_distro_request(
            "testy",
            "C51",
            "badpkg",
            ["ab/1.2"],
            "joe",
            ppas=["team/overlay", "joe/goodstuff"],
        )
        self.assertEqual(mock_urlopen.call_count, 5)

    @patch("request.submit.urllib.request.urlopen")
    def test_nonexisting_trigger_ppa(self, mock_urlopen):
        """Trigger source package/version does not exist in PPA."""
        # mock Launchpad response: successful form, but no matching
        # source/version
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 200
        cm.geturl.return_value = "http://mock.launchpad.net"
        cm.read.side_effect = [
            b'{"name": "overlay"}',
            b'{"name": "goodstuff"}',
            b'{"entries": []}',
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy",
                "C51",
                "blue",
                ["ab/1.2"],
                "joe",
                ppas=["team/overlay", "joe/goodstuff"],
            )
        self.assertEqual(
            str(cme.exception),
            "ab/1.2 is not published in PPA joe/goodstuff testy",
        )
        self.assertEqual(mock_urlopen.call_count, 3)

    @patch("request.submit.urllib.request.urlopen")
    def test_no_upload_perm(self, mock_urlopen):
        """Requester is not allowed to upload package."""
        # mock Launchpad response: successful form, matching
        # source/version, upload not allowed
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.side_effect = [200, 200, 403, 403, 200]
        cm.read.side_effect = [
            b'{"entries": [{"component_name": "main"}]}',
            b'{"entries": [{"component_name": "main"}]}',
            b"false",
            b"false",
            b'{"entries": []}',
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1.2"], "joe"
            )
        self.assertIn("not allowed to upload blue or ab", str(cme.exception))
        self.assertEqual(mock_urlopen.call_count, 5)

    @patch("request.submit.urllib.request.urlopen")
    def test_distro_ok(self, mock_urlopen):
        """Valid distro request is accepted."""
        # mock Launchpad response: successful form, matching
        # source/version, upload allowed
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 200
        cm.read.side_effect = [
            b'{"entries": [{"component_name": "main"}]}',
            b'{"entries": [{"component_name": "main"}]}',
            b"true",
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        self.submit.validate_distro_request("testy", "C51", "blue", ["ab/1.2"], "joe")
        self.assertEqual(mock_urlopen.call_count, 3)

    @patch("request.submit.urllib.request.urlopen")
    def test_distro_all_proposed(self, mock_urlopen):
        """Valid distro request with all-proposed is accepted."""
        # mock Launchpad response: successful form, matching
        # source/version, upload allowed
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 200
        cm.read.side_effect = [
            b'{"entries": [{"component_name": "main"}]}',
            b'{"entries": [{"component_name": "main"}]}',
            b"true",
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        self.submit.validate_distro_request(
            "testy", "C51", "blue", ["ab/1.2"], "joe", **{"all-proposed": "1"}
        )
        self.assertEqual(mock_urlopen.call_count, 3)

    def test_distro_all_proposed_bad_value(self):
        """Valid distro request with invalid all-proposed value."""
        with self.assertRaises(BadRequest) as cme:
            self.submit.validate_distro_request(
                "testy", "C51", "blue", ["ab/1.2"], "joe", **{"all-proposed": "bogus"}
            )
        self.assertIn("nvalid all-proposed value", str(cme.exception))

    @patch("request.submit.urllib.request.urlopen")
    def test_validate_distro_whitelisted_team(self, mock_urlopen):
        """Valid distro request via whitelisted team is accepted."""
        # mock Launchpad response: published sources, denied uploads, allowed team
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.side_effect = [200, 200, 403, 403, 200]
        cm.read.side_effect = [
            b'{"entries": [{"component_name": "main"}]}',
            b'{"entries": [{"component_name": "main"}]}',
            b"false",
            b"false",
            b'{"entries": [{"name": "list"}]}',
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        self.submit.validate_distro_request("testy", "C51", "blue", ["ab/1.2"], "joe")
        self.assertEqual(mock_urlopen.call_count, 5)

    @patch("request.submit.urllib.request.urlopen")
    def test_ppa_ok(self, mock_urlopen):
        """Valid PPA request is accepted."""
        # mock Launchpad response: successful form, matching
        # source/version, upload allowed
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 200
        cm.read.side_effect = [
            b'{"name": "1.10-4ubuntu4.1"}',
            b'{"entries": [{"component_name": "main"}]}',
            b'{"entries": [{"component_name": "main"}]}',
            b"true",
        ]
        cm.return_value = cm
        mock_urlopen.return_value = cm

        self.submit.validate_distro_request(
            "testy",
            "C51",
            "blue",
            ["ab/1.2"],
            "joe",
            ppas=["blue/1.10-4ubuntu4.1"],
        )
        self.assertEqual(mock_urlopen.call_count, 4)

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
        cm = MagicMock()
        cm.__enter__.return_value = cm
        cm.getcode.return_value = 404
        cm.geturl.return_value = "http://mock.launchpad.net"
        cm.read.return_value = b"{}"
        cm.return_value = cm
        mock_urlopen.return_value = cm

        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy", "C51", "ab", ["bad/ppa"], **{"build-git": "https://x.com/proj"}
            )
        self.assertEqual(str(cme.exception), "ppa bad/ppa not found")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("request.submit.Submit.is_valid_ppa")
    def test_bad_env(self, is_valid_ppa):
        is_valid_ppa.return_value = True
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy",
                "C51",
                "ab",
                env=["foo=1", "bar=1\n="],
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

    @patch("request.submit.Submit.is_valid_ppa")
    def test_bad_git_url(self, is_valid_ppa):
        is_valid_ppa.return_value = True
        with self.assertRaises(WebControlException) as cme:
            self.submit.validate_git_request(
                "testy",
                "C51",
                "ab",
                **{"build-git": "foo://x.com/proj", "ppas": ["a/b"]},
            )
        self.assertEqual(str(cme.exception), "Malformed build-git")

    @patch("request.submit.Submit.is_valid_ppa")
    def test_unknown_param(self, is_valid_ppa):
        is_valid_ppa.return_value = True
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

    @patch("request.submit.Submit.is_valid_ppa")
    def test_bad_testname(self, is_valid_ppa):
        is_valid_ppa.return_value = True
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

    @patch("request.submit.Submit.is_valid_ppa")
    def test_valid(self, is_valid_ppa):
        is_valid_ppa.return_value = True
        self.submit.validate_git_request(
            "testy",
            "C51",
            "ab",
            **{
                "build-git": "http://x.com/proj",
                "env": ["STATUS_URL=https://api.github.com/proj/123deadbeef"],
                "ppas": ["a/b"],
            },
        )

    @patch("request.submit.Submit.is_valid_ppa")
    def test_branch(self, is_valid_ppa):
        is_valid_ppa.return_value = True
        self.submit.validate_git_request(
            "testy",
            "C51",
            "ab",
            **{
                "build-git": "http://x.com/proj#refs/pull/2/head",
                "env": ["STATUS_URL=https://api.github.com/proj/123deadbeef"],
                "ppas": ["a/b"],
            },
        )

    @patch("request.submit.Submit.is_valid_ppa")
    def test_valid_testname(self, is_valid_ppa):
        is_valid_ppa.return_value = True
        self.submit.validate_git_request(
            "testy",
            "C51",
            "ab",
            **{
                "build-git": "http://x.com/proj",
                "testname": "first",
                "env": ["STATUS_URL=https://api.github.com/proj/123deadbeef"],
                "ppas": ["a/b"],
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


@patch("request.submit.amqp.Connection")
@patch("request.submit.amqp.Message")
def test_valid_request_context(self, message_con, mock_con):
    # mostly a passthrough, but ensure that we do wrap the string in Message()
    message_con.side_effect = lambda x: f">{x}<"

    self.submit.send_amqp_request(
        "testy",
        "C51",
        "foo",
        triggers=["ab/1"],
        requester="joe",
        context="ppa",
        ppas=["my/ppa"],
        notime=True,
    )
    mock_con.assert_called_once_with("1.2.3.4", userid="user", password="s3kr1t")
    cm_amqp_con = mock_con.return_value.__enter__.return_value
    cm_channel = cm_amqp_con.channel.return_value.__enter__.return_value
    cm_channel.basic_publish.assert_called_once_with(
        '>foo\n{"ppas": ["my/ppa"], "requester": "joe", "triggers": ["ab/1"]}<',
        routing_key="debci-ppa-testy-C51",
    )
