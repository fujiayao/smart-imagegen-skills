"""Offline behavioral tests. Test artifacts use the system temporary directory; all credentials are dummy."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import credentials
import gateway
import relay_gen

TEST_ROOT = Path(tempfile.gettempdir()) / "smart-imagegen-skills-tests"
TEST_ROOT.mkdir(parents=True, exist_ok=True)
DUMMY = "dummy-unit-test-key-not-a-real-secret"


class FakeResponse:
    def __init__(self, data):
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return self.raw[:limit]


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.root = Path(self.temp.name)
        assert self.root.resolve().parent == TEST_ROOT.resolve()
        self.home = self.root / "codex"
        self.home.mkdir()
        self.db = self.root / "cc.db"
        self.config()
    def tearDown(self):
        self.temp.cleanup()
    def config(self, text="", auth=None):
        (self.home / "config.toml").write_text(text, encoding="utf-8")
        (self.home / "auth.json").write_text(json.dumps(auth or {}), encoding="utf-8")
    def add_cc(self, name, current=False, models=None, base="https://relay.example/v1", key=DUMMY):
        config = f'model_provider="relay"\n[model_providers.relay]\nbase_url="{base}"'
        data = {"auth": {"OPENAI_API_KEY": key}, "config": config, "modelCatalog": {"models": models or []}}
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute("CREATE TABLE IF NOT EXISTS providers(id INTEGER PRIMARY KEY, name TEXT, is_current INTEGER, settings_config TEXT, app_type TEXT)")
            con.execute("INSERT INTO providers(name,is_current,settings_config,app_type) VALUES (?,?,?,?)", (name, int(current), json.dumps(data), "codex"))
            con.commit()
    def route(self, **kw):
        return gateway.resolve(home=self.home, db=self.db, **kw)
    def test_chatgpt_never_reads_cc_or_saved(self):
        self.config(auth={"auth_mode": "chatgpt", "OPENAI_API_KEY": "stale-key", "tokens": {"access_token": "oauth-not-api"}})
        with patch.object(gateway, "cc_gateways", side_effect=AssertionError("must not read")):
            r = self.route(saved_loader=Mock(side_effect=AssertionError("must not read")))
        self.assertEqual(r.mode, "native")
        self.assertIsNone(r.gateway)
    def test_legacy_tokens_detect_native(self):
        self.config(auth={"tokens": {"access_token": "oauth-not-api"}})
        self.assertEqual(self.route().mode, "native")
    def test_custom_provider_does_not_reuse_oauth(self):
        self.config('model_provider="x"\n[model_providers.x]\nbase_url="https://relay.example/v1"', {"tokens": {"access_token": "oauth-not-api"}})
        r = self.route()
        self.assertEqual(r.mode, "relay")
        self.assertEqual(r.gateway.api_key, "")
    def test_selected_toml_table_not_first_table(self):
        self.config('model_provider="b"\n[model_providers.a]\nbase_url="https://wrong.example/v1"\n[model_providers.b]\nbase_url="https://right.example/api/v1"\nexperimental_bearer_token="dummy-selected-token"')
        self.assertEqual(self.route().gateway.base_url, "https://right.example/api/v1")
    def test_profile_overrides_selected_provider(self):
        self.config('model_provider="a"\nprofile="work"\n[profiles.work]\nmodel_provider="b"\n[model_providers.a]\nbase_url="https://wrong.example/v1"\n[model_providers.b]\nbase_url="https://right.example/v1"\nexperimental_bearer_token="dummy-token"')
        self.assertEqual(self.route().gateway.base_url, "https://right.example/v1")
    def test_env_key_is_paired_with_selected_provider(self):
        self.config('model_provider="x"\n[model_providers.x]\nbase_url="https://right.example/v1"\nenv_key="UNIT_IMAGE_KEY"\nrequires_openai_auth=false')
        with patch.dict(os.environ, {"UNIT_IMAGE_KEY": DUMMY}):
            self.assertEqual(self.route().gateway.api_key, DUMMY)
    def test_no_cross_gateway_key_borrow(self):
        self.config('model_provider="x"\n[model_providers.x]\nbase_url="http://127.0.0.1:15721/v1"')
        self.add_cc("current", True, ["gpt-image-2"])
        self.assertEqual(self.route().gateway.api_key, "")
    def test_exact_url_cc_key_reuse(self):
        self.config('model_provider="x"\n[model_providers.x]\nbase_url="https://relay.example/v1/"')
        self.add_cc("current", True, ["gpt-image-2"])
        self.assertEqual(self.route().gateway.api_key, DUMMY)
    def test_current_cc_without_model_not_replaced(self):
        self.add_cc("chat-current", True, ["gpt-chat"])
        self.add_cc("image-other", False, ["gpt-image-2"], "https://other.example/v1")
        r = self.route(from_cc=True)
        self.assertEqual(r.gateway.source, "cc-switch:chat-current")
        self.assertEqual(r.alternatives[0]["provider"], "image-other")
    def test_explicit_provider_no_global_switch(self):
        self.add_cc("current", True, ["gpt-chat"])
        self.add_cc("image", False, ["gpt-image-2"], "https://other.example/v1")
        before = self.db.read_bytes()
        self.assertEqual(self.route(provider="image").gateway.source, "cc-switch:image")
        self.assertEqual(before, self.db.read_bytes())
    def test_from_cc_ignores_invalid_codex(self):
        self.config("not valid toml")
        self.add_cc("current", True, ["gpt-image-2"])
        self.assertEqual(self.route(from_cc=True).mode, "relay")
    def test_explicit_json_failure_no_fallback(self):
        self.add_cc("current", True, ["gpt-image-2"])
        with self.assertRaises(gateway.ConfigError):
            self.route(config=self.root / "missing.json")
    def test_explicit_json_valid_overrides_chatgpt(self):
        self.config(auth={"auth_mode": "chatgpt"})
        p = self.root / "dummy-config.json"
        p.write_text(json.dumps({"api_key": DUMMY, "base_url": "https://image.example", "model": "gpt-image-2"}))
        self.assertEqual(self.route(config=p).gateway.base_url, "https://image.example/v1")
    def test_two_current_providers_fail_closed(self):
        self.add_cc("a", True)
        self.add_cc("b", True)
        with self.assertRaises(gateway.ConfigError):
            self.route(from_cc=True)
    def test_explicit_native_no_files_needed(self):
        r = gateway.resolve(home=self.root / "absent", db=self.db, mode="native")
        self.assertEqual(r.mode, "native")
    def test_missing_both_modes_is_unknown(self):
        # No custom provider, no account, no API key: default official URL alone isn't a login.
        r = self.route()
        self.assertNotEqual(r.mode, "native")
        self.assertIsNone(r.gateway)


class DiscoveryTests(unittest.TestCase):
    def route(self, catalog=None, key=DUMMY):
        return gateway.Route("relay", gateway.Gateway("https://unit.example/v1", key, "unit", catalog or []))
    def discover(self, data, route=None, **kw):
        opener = Mock()
        opener.open.return_value = FakeResponse(data)
        r = gateway.discover(route or self.route(), opener=opener, **kw)
        return r, opener
    def test_live_models_success(self):
        r, op = self.discover({"data": [{"id": "gpt-chat"}, {"id": "gpt-image-2.5"}, {"id": "gpt-image-2"}]})
        self.assertEqual(r["model"], "gpt-image-2")
        request = op.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://unit.example/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + DUMMY)
        self.assertNotIn(DUMMY, json.dumps(r))
    def test_local_gateway_slug_schema(self):
        r, _ = self.discover({"models": [{"slug": "gpt-image-2"}]})
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["evidence"], "live_gateway_models_list")
    def test_live_empty_overrides_catalog(self):
        r, _ = self.discover({"data": []}, self.route(["gpt-image-2"]))
        self.assertEqual(r["status"], "need_image_model")
        self.assertIsNone(r["model"])
    def test_vision_input_not_generation(self):
        r, _ = self.discover({"data": [{"id": "vision-image-embedding", "input_modalities": ["image"]}]})
        self.assertEqual(r["status"], "need_image_model")
    def test_vendor_image_models_require_adapter(self):
        r, _ = self.discover({"data": [{"id": "flux-pro"}]})
        self.assertEqual(r["status"], "unsupported_image_api")
    def test_output_image_metadata_is_recognized(self):
        r, _ = self.discover({"data": [{"id": "vendor-painter", "architecture": {"output_modalities": ["image"]}}]})
        self.assertEqual(r["status"], "unsupported_image_api")
    def test_invalid_model_schema_is_not_empty(self):
        for data in (b"not json", {"error": "nope"}, {"data": [{"random": "x"}]}):
            r, _ = self.discover(data)
            self.assertEqual(r["status"], "invalid_model_list")
    def test_explicit_model_absent_no_substitution(self):
        r, _ = self.discover({"data": [{"id": "gpt-image-2"}]}, model="gpt-image-2.5")
        self.assertEqual(r["status"], "model_not_listed")
        self.assertIsNone(r["model"])
    def test_no_credentials_no_network(self):
        op = Mock(side_effect=AssertionError("no network"))
        r = gateway.discover(self.route(key=""), opener=op)
        self.assertEqual(r["status"], "need_credentials")
        op.open.assert_not_called()
    def test_http_failures_distinguished(self):
        for code, status in ((401, "authentication_failed"), (403, "permission_denied"), (404, "model_list_unavailable"), (405, "model_list_unavailable"), (429, "rate_limited"), (500, "gateway_error")):
            with self.subTest(code=code):
                op = Mock()
                op.open.side_effect = urllib.error.HTTPError("https://unit.example", code, "unit", {}, None)
                r = gateway.discover(self.route(["gpt-image-2"]), opener=op)
                self.assertEqual(r["status"], status)
                self.assertNotEqual(r["status"], "ready")
    def test_timeout_not_missing_model(self):
        op = Mock()
        op.open.side_effect = TimeoutError()
        self.assertEqual(gateway.discover(self.route(), opener=op)["status"], "network_error")
    def test_redirect_does_not_forward_authorization(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302 if self.path.endswith("/models") else 200)
                self.send_header("Location", "/destination")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            g = gateway.Gateway(f"http://127.0.0.1:{server.server_port}/v1", DUMMY)
            r = gateway.discover(gateway.Route("relay", g), timeout=2)
            self.assertEqual(r["http_status"], 302)
            self.assertEqual(hits, ["/v1/models"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class SafetyTests(unittest.TestCase):
    def test_secure_base_urls(self):
        for value in ("http://remote.example/v1", "https://user:pass" + "@" + "remote.example/v1", "https://remote.example/v1?api_key=dummy", "https://remote.example/v1#x", "not-a-url"):
            with self.subTest(url=value), self.assertRaises(gateway.ConfigError):
                gateway.norm_base(value)
        self.assertEqual(gateway.norm_base("http://127.0.0.1:1234"), "http://127.0.0.1:1234/v1")
        self.assertEqual(gateway.norm_base("https://relay.example/custom/v1/"), "https://relay.example/custom/v1")
    def test_gateway_repr_no_secret(self):
        self.assertNotIn(DUMMY, repr(gateway.Gateway("https://unit.example/v1", DUMMY)))
    def test_all_secret_flags_rejected(self):
        for value in ("--api-key", "--api_key=dummy", "--token=dummy", "-k"):
            with self.assertRaises(gateway.ConfigError):
                relay_gen.reject_secret_flags(["generate", value])
    def test_model_override_conflict_blocked(self):
        with self.assertRaises(gateway.ConfigError):
            relay_gen.engine_model(["generate", "--model=x"], "gpt-image-2")
    def test_default_output_is_portable_and_no_secret_command(self):
        cmd = relay_gen.prepare_command(["generate", "--prompt", "dummy prompt"], "gpt-image-2", Path("unit-engine.py"))
        self.assertEqual(Path(cmd[-1]).parent, relay_gen.OUTPUT)
        self.assertNotIn(DUMMY, str(cmd))
    def test_explicit_output_is_preserved(self):
        destination = TEST_ROOT / "explicit-output.png"
        cmd = relay_gen.prepare_command(["generate", "--out", str(destination)], "gpt-image-2", Path("unit.py"))
        self.assertIn(str(destination), cmd)
    def test_hidden_input_refuses_pipe(self):
        with patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(gateway.ConfigError):
                credentials.hidden_input()
    def test_emit_redacts_before_json_escaping(self):
        key = 'dummy"key'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relay_gen.emit({"unit": "echo " + key}, key)
        self.assertEqual(json.loads(out.getvalue())["unit"], "echo [REDACTED]")
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_dpapi_round_trip_no_plaintext(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            path = Path(folder) / "dummy.dpapi"
            g = gateway.Gateway("https://unit.example/v1", DUMMY, requested_model="gpt-image-2")
            credentials.save_gateway(path, g)
            self.assertNotIn(DUMMY.encode(), path.read_bytes())
            recovered = credentials.load_gateway(path)
            self.assertEqual(recovered.api_key, DUMMY)
            self.assertEqual(recovered.requested_model, "gpt-image-2")
    def test_invalid_dpapi_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            path = Path(folder) / "bad.dpapi"
            path.write_bytes(b"dummy corrupted ciphertext")
            with self.assertRaises(gateway.ConfigError):
                credentials.load_gateway(path)
    def test_preflight_rejects_existing_output_before_popen(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            p = Path(folder) / "existing.png"
            p.write_bytes(b"dummy image")
            fake = Mock(returncode=0, stdout=json.dumps({"model": "gpt-image-2", "outputs": [str(p)]}), stderr="")
            with patch.object(relay_gen.subprocess, "run", return_value=fake), self.assertRaises(gateway.ConfigError):
                relay_gen.preflight(["unit-engine"], {}, "gpt-image-2", DUMMY)
    def test_batch_missing_output_or_other_model_rejected(self):
        for record in ({"model": "gpt-image-2", "outputs": []}, {"model": "gpt-image-2.5", "outputs": [str(TEST_ROOT / "unit.png")]}):
            fake = Mock(returncode=0, stdout=json.dumps(record), stderr="")
            with patch.object(relay_gen.subprocess, "run", return_value=fake), self.assertRaises(gateway.ConfigError):
                relay_gen.preflight(["unit-engine"], {}, "gpt-image-2", DUMMY)
    def test_offline_check_never_queries_api(self):
        r = gateway.Route("relay", gateway.Gateway("https://unit.example/v1", DUMMY))
        with patch.object(relay_gen, "resolve", return_value=r), patch.object(relay_gen, "discover", side_effect=AssertionError("offline")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(relay_gen.main(["--check"]), 0)
    def test_native_cli_handoff_never_invokes_engine(self):
        with patch.object(relay_gen.subprocess, "Popen", side_effect=AssertionError("no engine")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(relay_gen.main(["--mode", "native"]), 0)
        self.assertEqual(json.loads(output.getvalue())["next_action"], "use_builtin_image_gen")
    def test_no_model_blocks_paid_call(self):
        r = gateway.Route("relay", gateway.Gateway("https://unit.example/v1", DUMMY))
        report = {"mode": "relay", "status": "need_image_model", "model": None}
        with patch.object(relay_gen, "resolve", return_value=r), patch.object(relay_gen, "discover", return_value=report), patch.object(relay_gen.subprocess, "Popen", side_effect=AssertionError("paid call")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(relay_gen.main(["generate", "--prompt", "dummy prompt"]), 2)
    def test_permission_failure_cannot_allow_unverified(self):
        r = gateway.Route("relay", gateway.Gateway("https://unit.example/v1", DUMMY))
        with patch.object(relay_gen, "resolve", return_value=r), patch.object(relay_gen, "discover", return_value={"status": "permission_denied"}), patch.object(relay_gen.subprocess, "Popen", side_effect=AssertionError("paid call")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(relay_gen.main(["--allow-unverified", "--image-model", "gpt-image-2", "generate", "--prompt", "dummy"]), 2)
    def test_hidden_input_bypasses_corrupt_saved_config(self):
        fake = gateway.Gateway("https://unit.example/v1", DUMMY)
        def resolver(**kwargs):
            self.assertIsNone(kwargs["saved_loader"])
            return gateway.Route("relay", fake)
        with patch.object(relay_gen, "resolve", side_effect=resolver), patch.object(relay_gen, "hidden_input", return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(relay_gen.main(["--credential-input", "--check"]), 0)


class IntegrationTests(unittest.TestCase):
    def run_mock_generation(self, report, args, hidden=False):
        g = gateway.Gateway("https://unit.example/v1", DUMMY)
        route = gateway.Route("relay", g)
        process = Mock()
        process.stdout = iter(["unit upstream echoed " + DUMMY + "\n"])
        process.wait.return_value = 0
        out = io.StringIO()
        with patch.object(relay_gen, "resolve", return_value=route), patch.object(relay_gen, "hidden_input", return_value=g), patch.object(relay_gen, "discover", return_value=report), patch.object(relay_gen, "preflight") as preflight, patch.object(relay_gen.subprocess, "Popen", return_value=process) as popen, patch.object(relay_gen, "save_gateway") as save, contextlib.redirect_stdout(out):
            rc = relay_gen.main(args)
        self.assertEqual(rc, 0)
        preflight.assert_called_once()
        save.assert_not_called()
        self.assertEqual(popen.call_args.kwargs["env"]["OPENAI_API_KEY"], DUMMY)
        self.assertEqual(popen.call_args.kwargs["env"]["OPENAI_BASE_URL"], "https://unit.example/v1")
        self.assertNotIn(DUMMY, str(popen.call_args.args[0]))
        self.assertNotIn(DUMMY, out.getvalue())
        self.assertIn("[REDACTED]", out.getvalue())
    def test_hidden_input_then_generation_in_same_process(self):
        self.run_mock_generation({"status": "ready", "model": "gpt-image-2"}, ["--credential-input", "generate", "--prompt", "dummy prompt"])
    def test_confirmed_unlisted_endpoint_trial(self):
        self.run_mock_generation({"status": "model_list_unavailable"}, ["--allow-unverified", "--image-model", "gpt-image-2", "generate", "--prompt", "dummy prompt"])
    @unittest.skipUnless(relay_gen.ENGINE.is_file(), "Bundled engine required")
    def test_real_engine_valid_dry_run_no_api(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            cmd = relay_gen.prepare_command(["generate", "--prompt", "dummy prompt", "--size", "1152x2048", "--out", str(Path(folder)/"not-generated.png")], "gpt-image-2", relay_gen.ENGINE)
            relay_gen.preflight(cmd, {**os.environ, "OPENAI_API_KEY": DUMMY, "OPENAI_BASE_URL": "https://unit.invalid/v1"}, "gpt-image-2", DUMMY)
            self.assertFalse((Path(folder)/"not-generated.png").exists())
    @unittest.skipUnless(relay_gen.ENGINE.is_file(), "Bundled engine required")
    def test_real_engine_invalid_size_stops_before_api(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            cmd = relay_gen.prepare_command(["generate", "--prompt", "dummy prompt", "--size", "3x5", "--out", str(Path(folder)/"not-generated.png")], "gpt-image-2", relay_gen.ENGINE)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(gateway.ConfigError):
                relay_gen.preflight(cmd, {**os.environ, "OPENAI_API_KEY": DUMMY, "OPENAI_BASE_URL": "https://unit.invalid/v1"}, "gpt-image-2", DUMMY)
    @unittest.skipUnless(relay_gen.ENGINE.is_file(), "Bundled engine required")
    def test_real_engine_batch_dry_run_two_jobs(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            jobs = Path(folder)/"dummy-jobs.jsonl"
            jobs.write_text('"dummy scene one"\n"dummy scene two"\n', encoding="utf-8")
            out = Path(folder)/"not-created"
            cmd = relay_gen.prepare_command(["generate-batch", "--input", str(jobs), "--out-dir", str(out)], "gpt-image-2", relay_gen.ENGINE)
            relay_gen.preflight(cmd, {**os.environ, "OPENAI_API_KEY": DUMMY, "OPENAI_BASE_URL": "https://unit.invalid/v1"}, "gpt-image-2", DUMMY)
            self.assertEqual(list(out.glob("*")), [])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
