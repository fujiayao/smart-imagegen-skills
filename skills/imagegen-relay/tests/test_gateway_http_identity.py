"""Offline HTTP identity regressions: in-memory transport, dummy credentials only."""
import contextlib
import io
import json
import sys
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gateway

DUMMY = "dummy-http-identity-key"
URL = "https://unit.invalid/v1/models"
UA = "imagegen-relay/1.0"


def headers(**values):
    result = Message()
    for name, value in values.items():
        result[name.replace("_", "-")] = value
    return result


class TrackedBody(io.BytesIO):
    def __init__(self, value, *, fail_read=False, fail_close=False):
        super().__init__(value)
        self.limits = []
        self.fail_read = fail_read
        self.fail_close = fail_close

    def read(self, limit=-1):
        self.limits.append(limit)
        if self.fail_read:
            raise OSError("private upstream detail " + DUMMY)
        return super().read(limit)

    def close(self):
        super().close()
        if self.fail_close:
            self.fail_close = False
            raise OSError("private close detail " + DUMMY)


class IdentityTransport(urllib.request.HTTPSHandler):
    """Exercise urllib's real default headers without opening any socket."""
    def __init__(self):
        super().__init__()
        self.requests = []

    def https_open(self, request):
        self.requests.append(request)
        if request.get_header("User-agent") != UA:
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", headers(Server="cloudflare"),
                io.BytesIO(b"error code: 1010"),
            )
        response = urllib.response.addinfourl(
            io.BytesIO(b'{"data":[{"id":"gpt-image-2"}]}'),
            headers(Content_Type="application/json"), request.full_url, 200,
        )
        response.msg = "OK"
        return response


class GatewayHttpIdentityTests(unittest.TestCase):
    def route(self):
        return gateway.Route("relay", gateway.Gateway("https://unit.invalid/v1", DUMMY))

    def failure(self, code=403, body=b"Error 1010", response_headers=None, **stream_options):
        stream = TrackedBody(body, **stream_options)
        error = urllib.error.HTTPError(
            URL, code, "Forbidden", response_headers if response_headers is not None else headers(Server="cloudflare"), stream,
        )
        self.addCleanup(lambda: (setattr(stream, "fail_close", False), error.close()))
        opener = Mock()
        opener.open.side_effect = error
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            report = gateway.discover(self.route(), opener=opener)
        opener.open.assert_called_once()
        self.assertTrue(stream.closed, "HTTPError response must always be closed")
        output = json.dumps(report) + stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(DUMMY, output)
        self.assertNotIn("private", output)
        return report, stream

    def test_default_urllib_identity_blocked_honest_identity_succeeds_once(self):
        transport = IdentityTransport()
        opener = urllib.request.build_opener(gateway.NoRedirect(), transport)
        old_request = urllib.request.Request(URL, headers={"Authorization": "Bearer " + DUMMY, "Accept": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(old_request)
        caught.exception.close()
        self.assertTrue(transport.requests[0].get_header("User-agent").startswith("Python-urllib/"))
        transport.requests.clear()
        report = gateway.discover(self.route(), opener=opener)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["model"], "gpt-image-2")
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_header("User-agent"), UA)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + DUMMY)
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_cloudflare_1010_is_safe_diagnostic_not_auth_failure(self):
        for body in (b"Error1010", b"error code: 1010", b"<h1>Error <span>1010</span></h1>"):
            for signal in (headers(Server="Cloudflare"), headers(CF_Ray="private-ray")):
                with self.subTest(body=body, signal=list(signal.keys())):
                    report, stream = self.failure(body=body + b" private " + DUMMY.encode(), response_headers=signal)
                    self.assertEqual(report["status"], "permission_denied")
                    self.assertEqual(report["http_status"], 403)
                    self.assertEqual(report["diagnostic"], "cloudflare_1010")
                    self.assertEqual(report["next_action"], "diagnose_gateway_client_identity_or_edge_policy")
                    self.assertEqual(stream.limits, [8192])
                    self.assertNotIn("model", report)

    def test_cloudflare_signal_and_explicit_error_code_both_required(self):
        cases = (
            (headers(), b"Error 1010"),
            (headers(Server="nginx"), b"Cloudflare Error 1010"),
            (headers(Server="not-cloudflare"), b"Error 1010"),
            (headers(CF_Ray=""), b"Error 1010"),
            (headers(Server="cloudflare"), b"Permission denied"),
            (headers(Server="cloudflare"), b"request id 1010"),
            (headers(Server="cloudflare"), b"Error 10100"),
            (headers(Server="cloudflare"), b"Error 11010"),
            (headers(Server="cloudflare"), b"Error 1020"),
        )
        for signal, body in cases:
            with self.subTest(body=body, signal=list(signal.items())):
                report, _ = self.failure(body=body, response_headers=signal)
                self.assertEqual(report["status"], "permission_denied")
                self.assertNotIn("diagnostic", report)
                self.assertEqual(report["next_action"], "diagnose_or_confirm_explicit_model")

    def test_reads_at_most_8k_and_never_follows_late_marker(self):
        report, stream = self.failure(body=b"x" * 8192 + b" Error 1010 private " + DUMMY.encode())
        self.assertEqual(stream.limits, [8192])
        self.assertNotIn("diagnostic", report)

    def test_read_and_close_failures_safely_preserve_403(self):
        for options in ({"fail_read": True}, {"fail_read": True, "fail_close": True}):
            with self.subTest(options=options):
                report, stream = self.failure(**options)
                self.assertEqual(report["status"], "permission_denied")
                self.assertNotIn("diagnostic", report)
                self.assertEqual(stream.limits, [8192])

    def test_close_failure_does_not_discard_valid_diagnostic(self):
        report, _ = self.failure(fail_close=True)
        self.assertEqual(report["diagnostic"], "cloudflare_1010")

    def test_http_statuses_preserved_without_retry_or_non403_body_read(self):
        for code, status in ((401, "authentication_failed"), (403, "permission_denied"), (404, "model_list_unavailable"), (405, "model_list_unavailable"), (501, "model_list_unavailable"), (429, "rate_limited"), (500, "gateway_error"), (302, "gateway_error")):
            with self.subTest(code=code):
                report, stream = self.failure(code=code, body=b"private " + DUMMY.encode())
                self.assertEqual(report["status"], status)
                self.assertEqual(report["http_status"], code)
                self.assertNotIn("diagnostic", report)
                self.assertEqual(stream.limits, [8192] if code == 403 else [])

    def test_non403_with_cloudflare_body_not_misclassified(self):
        for code in (401, 429, 500):
            with self.subTest(code=code):
                report, stream = self.failure(code=code)
                self.assertNotIn("diagnostic", report)
                self.assertEqual(stream.limits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
