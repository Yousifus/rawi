"""rawi_core.ipc: a real loopback round trip on an ephemeral port."""
import contextlib
import io
import socket
import threading
import unittest

import _helpers  # noqa: F401
from rawi_core import ipc


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def handler(req):
            if req.get("cmd") == "boom":
                raise ValueError("nope")
            return {"ok": True, "echo": req}

        ready = threading.Event()
        with contextlib.redirect_stdout(io.StringIO()):
            cls.info = ipc.serve(handler, port=0, ready=ready)
            assert ready.wait(5), "server did not start"
        cls.port = cls.info["port"]

    def test_a_command_name_is_a_request(self):
        r = ipc.call("status", port=self.port)
        self.assertEqual(r, {"ok": True, "echo": {"cmd": "status"}})

    def test_full_requests_and_unicode_survive(self):
        r = ipc.call({"cmd": "raw", "text": "مرحبا, hello"}, port=self.port)
        self.assertEqual(r["echo"]["text"], "مرحبا, hello")

    def test_handler_errors_come_back_as_replies(self):
        r = ipc.call("boom", port=self.port)
        self.assertFalse(r["ok"])
        self.assertIn("ValueError", r["error"])

    def test_malformed_request_is_treated_as_empty(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall(b"this is not json\n")
            reply = s.recv(4096)
        self.assertIn(b'"echo": {}', reply)

    def test_port_busy_sees_the_server(self):
        self.assertTrue(ipc.port_busy(self.port))


class TestNoServer(unittest.TestCase):
    def test_calling_nothing_is_an_answer_not_an_exception(self):
        r = ipc.call("status", timeout=0.5, port=free_port())
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_binding_a_taken_port_reports_instead_of_raising(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            ready = threading.Event()
            with contextlib.redirect_stdout(io.StringIO()):
                info = ipc.serve(lambda r: {}, port=blocker.getsockname()[1], ready=ready)
                self.assertTrue(ready.wait(5))
            # SO_REUSEADDR may let some systems share it; either outcome must be reported
            self.assertTrue("error" in info or "port" in info)
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
