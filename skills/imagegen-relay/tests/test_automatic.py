"""Automatic defaults; no remote service or real credentials are used."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prompt_search
import relay_gen
import test_components as components

TEST_ROOT = Path(tempfile.gettempdir()) / "smart-imagegen-skills-tests"
TEST_ROOT.mkdir(parents=True, exist_ok=True)


class AutomaticPromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.revision = "a" * 40
        for name, value in (("CACHE", self.root), ("component", Mock(return_value={"revision": self.revision}))):
            patcher = patch.object(prompt_search, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manifest = {"categories": [
            {"slug": "portrait", "title": "Portrait", "file": "portrait.json"},
            {"slug": "others", "title": "Uncategorized", "file": "others.json"},
            {"slug": "landscape", "title": "Landscape", "file": "landscape.json"}]}
        self.rows = [{"id": 1, "title": "Cat portrait", "content": "A cat in sunlight"}]
        self.directory = self.root / self.revision
    def save(self, filename, value):
        prompt_search.save_json(self.directory / filename, value)
    def fetch(self, filename, revision, **kwargs):
        self.assertEqual(revision, self.revision)
        self.assertIn("deadline", kwargs)
        return self.manifest if filename == "manifest.json" else self.rows
    def test_cold_cache_prepares_only_manifest_and_one_category(self):
        with patch.object(prompt_search, "fetch", side_effect=self.fetch) as fetch:
            result = prompt_search.automatic("cat portrait sunlight")
        self.assertEqual(result["status"], "matches")
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ["manifest.json", "portrait.json"])
        self.assertEqual(result["downloaded_categories"], ["portrait"])
        self.assertFalse((self.directory / "landscape.json").exists())
    def test_warm_cache_never_networks(self):
        self.save("manifest.json", self.manifest)
        self.save("portrait.json", self.rows)
        with patch.object(prompt_search, "fetch", side_effect=AssertionError("network not needed")):
            result = prompt_search.automatic("cat portrait")
        self.assertEqual(result["status"], "matches")
        self.assertEqual(result["downloaded_categories"], [])
    def test_manifest_download_failure_continues_without_repair_request(self):
        with patch.object(prompt_search, "fetch", side_effect=TimeoutError("private query")):
            result = prompt_search.automatic("cat portrait")
        self.assertEqual(result["next_action"], "optimize_directly_without_examples")
        self.assertNotIn("private query", json.dumps(result))
    def test_category_download_failure_continues(self):
        self.save("manifest.json", self.manifest)
        with patch.object(prompt_search, "fetch", side_effect=OSError("offline")):
            result = prompt_search.automatic("cat portrait")
        self.assertEqual(result["status"], "prompt_search_unavailable")
        self.assertFalse((self.directory / "portrait.json").exists())
    def test_no_match_is_successful_direct_optimization(self):
        self.save("manifest.json", self.manifest)
        self.save("others.json", self.rows)
        result = prompt_search.automatic("nonmatchingkeyword")
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["next_action"], "optimize_directly_without_examples")
    def test_unspecified_category_does_not_default_to_a_platform(self):
        self.assertEqual(prompt_search.choose_category(self.manifest, "ceramic teapot")["slug"], "others")
    def test_category_is_selected_from_dynamic_manifest(self):
        self.manifest["categories"].append({"slug": "architecture", "title": "Architecture", "file": "architecture.json"})
        self.assertEqual(prompt_search.choose_category(self.manifest, "architecture drawing")["slug"], "architecture")
    def test_explicit_categories_still_download_at_most_one(self):
        with patch.object(prompt_search, "fetch", side_effect=self.fetch) as fetch:
            result = prompt_search.automatic("cat portrait", ["portrait", "landscape"])
        self.assertEqual(len(fetch.call_args_list), 2)
        self.assertEqual(result["downloaded_categories"], ["portrait"])
    def test_corrupt_manifest_falls_back(self):
        self.save("manifest.json", {"categories": [None]})
        self.assertEqual(prompt_search.automatic("cat")["status"], "prompt_search_unavailable")
    def test_corrupt_category_falls_back(self):
        self.save("manifest.json", self.manifest)
        self.save("portrait.json", {"wrong": []})
        self.assertEqual(prompt_search.automatic("portrait")["status"], "prompt_search_unavailable")
    def test_invalid_download_not_cached(self):
        self.save("manifest.json", self.manifest)
        with patch.object(prompt_search, "fetch", return_value={}):
            self.assertEqual(prompt_search.automatic("portrait")["status"], "prompt_search_unavailable")
        self.assertFalse((self.directory / "portrait.json").exists())
    def test_bad_category_names_do_not_escape_cache(self):
        self.save("manifest.json", self.manifest)
        with patch.object(prompt_search, "fetch", side_effect=AssertionError("no fetch")):
            result = prompt_search.automatic("cat", ["../secret"])
        self.assertEqual(result["status"], "prompt_search_unavailable")
    def test_cli_auto_failure_returns_zero_with_next_action(self):
        with patch.object(prompt_search, "fetch", side_effect=TimeoutError), contextlib.redirect_stdout(io.StringIO()) as output:
            code = prompt_search.main(["--auto", "--query", "cat portrait"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["next_action"], "optimize_directly_without_examples")
    def test_auto_requires_query(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            prompt_search.main(["--auto"])
    def test_auto_rejects_manual_cache_commands(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            prompt_search.main(["--auto", "--query", "cat", "--sync-manifest"])
    def test_shared_download_deadline(self):
        with patch.object(prompt_search, "fetch", side_effect=self.fetch) as fetch:
            prompt_search.automatic("cat portrait")
        self.assertEqual(fetch.call_args_list[0].kwargs["deadline"], fetch.call_args_list[1].kwargs["deadline"])
    def test_elapsed_budget_prevents_network(self):
        with patch.object(prompt_search.time, "monotonic", return_value=10), patch.object(prompt_search, "urlopen", side_effect=AssertionError("no network")), self.assertRaises(TimeoutError):
            prompt_search.fetch("manifest.json", self.revision, deadline=9)
    def test_fetch_uses_only_fixed_public_path_not_user_content(self):
        class Response(io.BytesIO):
            pass
        payload = json.dumps(self.manifest).encode()
        with patch.object(prompt_search, "urlopen", return_value=Response(payload)) as open_url:
            self.assertEqual(prompt_search.fetch("manifest.json", self.revision), self.manifest)
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://raw.githubusercontent.com/YouMind-OpenLab/gpt-image-2-prompts-search/" + self.revision + "/references/manifest.json")
        self.assertIsNone(request.data)
        self.assertNotIn("Authorization", request.headers)


class AutomaticBackendTests(unittest.TestCase):
    def selected(self, *rest):
        return relay_gen.select_backend("auto", None, list(rest))[0]
    def test_single_generate_uses_awake(self):
        self.assertEqual(self.selected("generate", "--prompt", "cat"), "awakefantasy")
    def test_reference_edit_uses_awake(self):
        self.assertEqual(self.selected("edit", "--prompt", "cat", "--image", "a.png", "--image=b.png"), "awakefantasy")
    def test_one_output_uses_awake(self):
        self.assertEqual(self.selected("generate", "--prompt=cat", "--n=1"), "awakefantasy")
    def test_multiple_outputs_use_official_before_request(self):
        self.assertEqual(self.selected("generate", "--prompt", "cat", "--n", "2"), "official")
    def test_mask_uses_official_before_request(self):
        self.assertEqual(self.selected("edit", "--prompt", "cat", "--mask", "m.png"), "official")
    def test_batch_uses_official(self):
        self.assertEqual(self.selected("generate-batch", "--input", "batch.jsonl"), "official")
    def test_unknown_capability_is_not_silently_dropped(self):
        self.assertEqual(self.selected("generate", "--background=transparent", "--prompt", "cat"), "official")
    def test_explicit_override_kept(self):
        self.assertEqual(relay_gen.select_backend("official", None, ["generate"])[0], "official")
    def test_custom_engine_compatibility(self):
        self.assertEqual(relay_gen.select_backend("auto", "custom.py", ["generate"]), ("custom", Path("custom.py")))
    def test_conflicting_custom_engine_rejected(self):
        with self.assertRaises(relay_gen.ConfigError):
            relay_gen.select_backend("awakefantasy", "custom.py", ["generate"])
    def test_missing_flag_value_stays_in_preflight(self):
        self.assertEqual(self.selected("generate", "--prompt"), "official")
    def test_default_cli_plan_selects_awake_without_network(self):
        route = relay_gen.Route("relay", relay_gen.Gateway("https://example.invalid/v1", "dummy-auto-key", "test"))
        with patch.object(relay_gen, "resolve", return_value=route), patch.object(relay_gen, "discover", side_effect=AssertionError("must not network")), contextlib.redirect_stdout(io.StringIO()) as output:
            result = relay_gen.main(["--plan", "--image-model", "gpt-image-2", "generate", "--prompt", "cat"])
        self.assertEqual(result, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["backend"], "awakefantasy")
        self.assertIn("awake_engine.py", data["engine_command"][2])
    def test_implicit_invocation_is_explicitly_allowed(self):
        import yaml
        data = yaml.safe_load((ROOT / "agents/openai.yaml").read_text(encoding="utf-8"))
        self.assertTrue(data["policy"]["allow_implicit_invocation"])
        self.assertIn("$imagegen-relay", data["interface"]["default_prompt"])
    def test_entry_defaults_and_exceptions_are_documented(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for phrase in ("默认尝试 YouMind", "简单需求也", "原词执行", "精确局部改图", "不要求输入"):
            self.assertIn(phrase, text)


class DefaultLocalGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
    def test_default_cli_real_sdk_roundtrip_without_backend_flag(self):
        result, posts, _ = components.LocalGatewayTests.run_local(self, backend=None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(posts), 1)
        self.assertIn('"backend": "awakefantasy"', result.stdout)
        self.assertTrue((self.root / "image.meta.json").exists())
    def test_default_cli_429_does_not_switch_or_retry(self):
        result, posts, _ = components.LocalGatewayTests.run_local(self, status=429, backend=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(posts), 1)
        self.assertNotIn(components.DUMMY, result.stdout + result.stderr)
        self.assertFalse((self.root / "image.png").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
