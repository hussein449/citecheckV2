"""A run that brings its own model: provider, key, model name and endpoint.

The choice has to reach the model call for that run and no other, switch the
model tier on where the server has no key of its own, work with providers that
do not enforce OpenAI's strict output schema, and never let the key back out —
not in an error message, not in the report, not in a repr that ends up in a log.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from citecheck import match, pipeline

KEY = "sk-test-" + "x" * 40
SOURCE = (
    "Urban freight consolidation centres reduce delivery vehicle kilometres in "
    "dense city cores. The study follows three centres over eighteen months. "
) * 3
VERDICT = {"verdict": "supported", "confidence": 0.9, "reason": "Says so.",
           "evidence_quote": "Urban freight consolidation centres reduce delivery."}


def _reply(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=content))])


class ConfigTest(unittest.TestCase):
    def test_a_run_key_turns_the_model_tier_on_without_the_environment(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "CITECHECK_LLM": ""}):
            self.assertEqual(match.active_engine(), "lexical")
            self.assertEqual(match.active_engine(match.LLMConfig(api_key=KEY)), "openai")

    def test_the_operator_switch_still_wins(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "CITECHECK_LLM": "off"}):
            self.assertEqual(match.active_engine(match.LLMConfig(api_key=KEY)), "lexical")

    def test_another_provider_never_borrows_the_server_key(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-server-" + "y" * 30}):
            self.assertEqual(match.LLMConfig(provider="anthropic").key(), "")
            self.assertEqual(match.active_engine(match.LLMConfig(provider="anthropic")),
                             "lexical")

    def test_a_provider_is_called_at_its_own_address_with_its_default_model(self):
        llm = match.LLMConfig(provider="anthropic", api_key=KEY)
        self.assertEqual(llm.endpoint(), "https://api.anthropic.com/v1/")
        self.assertEqual(llm.model_name(), "claude-sonnet-5")
        self.assertEqual(match.LLMConfig(provider="gemini", api_key=KEY, model="gemini-x")
                         .model_name(), "gemini-x")

    def test_a_users_openai_key_skips_the_operators_gateway(self):
        with mock.patch.dict(os.environ, {"OPENAI_BASE_URL": "https://gateway.internal/v1"}):
            self.assertEqual(match.LLMConfig().endpoint(), "https://gateway.internal/v1")
            self.assertIsNone(match.LLMConfig(api_key=KEY).endpoint())

    def test_the_key_stays_out_of_repr_and_run_config(self):
        options = pipeline.Options(llm=match.LLMConfig(provider="anthropic", api_key=KEY))
        self.assertNotIn(KEY, repr(options))
        with mock.patch.dict(os.environ, {"CITECHECK_LLM": ""}):
            config = pipeline.run_config(options, time.time())
        self.assertNotIn(KEY, json.dumps(config))
        self.assertEqual(config["api_key_source"], "run")
        self.assertEqual(config["provider"], "anthropic")
        self.assertEqual(config["model"], "claude-sonnet-5")


class CallTest(unittest.TestCase):
    def _client(self, *effects):
        client = mock.MagicMock()
        client.chat.completions.create.side_effect = list(effects)
        return client

    def test_the_client_is_built_for_the_runs_provider(self):
        import openai

        llm = match.LLMConfig(provider="groq", api_key=KEY)
        with mock.patch.object(openai, "OpenAI") as client_cls:
            client_cls.return_value.chat.completions.create.return_value = _reply(json.dumps(VERDICT))
            result = match.openai_match("Centres cut vehicle kilometres.", SOURCE, llm=llm)
        self.assertEqual(client_cls.call_args.kwargs,
                         {"api_key": KEY, "base_url": "https://api.groq.com/openai/v1"})
        self.assertEqual(result.verdict, "supported")

    def test_a_rejected_schema_steps_down_to_json_mode(self):
        client = self._client(Exception("response_format json_schema is not supported"),
                              _reply(json.dumps(VERDICT)))
        match._complete(client, "m", "prompt", match.PROVIDERS["openai"])
        second = client.chat.completions.create.call_args_list[1].kwargs
        self.assertEqual(second["response_format"], {"type": "json_object"})
        self.assertIn("JSON object", second["messages"][0]["content"])

    def test_anthropic_gets_temperature_alone_and_an_output_cap(self):
        client = self._client(_reply(json.dumps(VERDICT)))
        match._complete(client, "claude-sonnet-5", "prompt", match.PROVIDERS["anthropic"])
        sent = client.chat.completions.create.call_args.kwargs
        self.assertEqual(sent["temperature"], 0)
        self.assertNotIn("top_p", sent)
        self.assertNotIn("response_format", sent)
        self.assertEqual(sent["max_tokens"], 1024)

    def test_the_strict_openai_call_is_unchanged(self):
        client = self._client(_reply(json.dumps(VERDICT)))
        match._complete(client, "gpt-4o", "prompt", match.PROVIDERS["openai"])
        sent = client.chat.completions.create.call_args.kwargs
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["messages"][0]["content"], match._JUDGE_SYSTEM)
        self.assertEqual((sent["temperature"], sent["top_p"]), (0, 1))
        self.assertNotIn("max_tokens", sent)

    def test_a_fenced_or_chatty_reply_still_parses(self):
        wrapped = "Here is my verdict:\n```json\n" + json.dumps(VERDICT) + "\n```"
        self.assertEqual(match._parse_json(wrapped)["verdict"], "supported")

    def test_judge_hands_the_config_to_every_call(self):
        seen = []
        llm = match.LLMConfig(provider="mistral", api_key=KEY)

        def fake_match(claim, source_text, title="", reference_line="",
                       abstract="", model=None, llm=None):
            seen.append(llm)
            return match.MatchResult(engine="openai", verdict="supported", score=0.9,
                                     reason="Judged.")

        with mock.patch.object(match, "openai_match", fake_match), \
                mock.patch.dict(os.environ, {"CITECHECK_LLM": ""}):
            match.judge(["One claim.", "Another claim."], SOURCE, llm=llm)
        self.assertEqual(seen, [llm, llm])


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch("citecheck.shots.browser_status", return_value=(False, "test")):
            import app
        cls.app = app

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "runs").mkdir()
        (root / "uploads").mkdir()
        self.started = []
        patches = [
            mock.patch.object(self.app, "RUNS_DIR", root / "runs"),
            mock.patch.object(self.app, "UPLOADS_DIR", root / "uploads"),
            # Capture the options instead of running a real pipeline.
            mock.patch.object(self.app, "_run_pipeline",
                              lambda *args: self.started.append(args[-1])),
            mock.patch.object(self.app, "_ALLOW_LOCAL_LLM", False),
            mock.patch.dict(os.environ, {"CITECHECK_LLM": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = self.app.app.test_client()

    def _upload(self, **fields):
        data = {"pdf": (io.BytesIO(b"%PDF-1.4"), "paper.pdf"), **fields}
        return self.client.post("/api/upload", data=data, content_type="multipart/form-data")

    def _options(self):
        for _ in range(50):
            if self.started:
                break
            time.sleep(0.02)
        return self.started[0]

    def test_the_page_offers_the_provider_picker_and_save_button(self):
        page = self.client.get("/").get_data(as_text=True)
        for marker in ('id="provider"', 'value="anthropic"', 'id="apiKey"',
                       'id="saveSettings"', 'id="resetSettings"'):
            self.assertIn(marker, page)

    def test_a_chosen_provider_reaches_the_run(self):
        res = self._upload(provider="anthropic", api_key=KEY, model="claude-opus-5-5")
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        llm = self._options().llm
        self.assertEqual((llm.provider, llm.api_key, llm.model),
                         ("anthropic", KEY, "claude-opus-5-5"))

    def test_blank_falls_back_to_the_server(self):
        self.assertEqual(self._upload().status_code, 200)
        self.assertTrue(self._options().llm.uses_server_key)

    def test_a_mangled_key_is_refused_without_being_echoed(self):
        res = self._upload(api_key="sk-half a sentence " + "z" * 30)
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("zzzz", res.get_data(as_text=True))
        self.assertFalse(self.started)

    def test_another_provider_needs_its_own_key(self):
        res = self._upload(provider="gemini")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Google Gemini", res.get_json()["error"])

    def test_an_unknown_provider_is_refused(self):
        self.assertEqual(self._upload(provider="skynet", api_key=KEY).status_code, 400)

    def test_a_custom_endpoint_on_a_private_network_is_refused(self):
        for url in ("https://127.0.0.1/v1", "https://169.254.169.254/latest",
                    "https://10.0.0.5/v1", "http://api.example.com/v1"):
            with self.subTest(url=url):
                res = self._upload(provider="custom", base_url=url, model="llama3")
                self.assertEqual(res.status_code, 400)

    def test_a_local_endpoint_is_allowed_once_opted_into(self):
        with mock.patch.object(self.app, "_ALLOW_LOCAL_LLM", True):
            res = self._upload(provider="custom", base_url="http://localhost:11434/v1",
                               model="llama3")
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        llm = self._options().llm
        self.assertEqual((llm.endpoint(), llm.model_name()),
                         ("http://localhost:11434/v1", "llama3"))

    def test_turning_the_model_off_ignores_the_provider_fields(self):
        self.assertEqual(self._upload(use_model="0", provider="skynet").status_code, 200)

    def test_the_check_endpoint_reports_without_the_key(self):
        with mock.patch.object(match, "check_connection",
                               return_value="Anthropic (Claude) rejected this API key."):
            res = self.client.post("/api/llm/check",
                                   data={"provider": "anthropic", "api_key": KEY})
        data = res.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("rejected", data["error"])
        self.assertNotIn(KEY, res.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
