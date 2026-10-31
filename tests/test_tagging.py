"""Task / pain-point tagging with a fake Anthropic client (no network, no key, no cost)."""
import json

from analysis_helpers import FakeClient, cfg_for, good_tag, response
from wbscraper.analysis.tagging import UNCLASSIFIED, TagCache, Tagger, cache_key, response_schema, system_prompt


def tagger(tmp_path, client, **over):
    cfg = cfg_for(tmp_path, **over)
    return Tagger(cfg, client, TagCache(tmp_path / "cache.json")), cfg


def test_request_uses_the_configured_model_enums_effort_and_a_cached_system_prompt(tmp_path):
    c = FakeClient()
    t, cfg = tagger(tmp_path, c)
    t.tag("I can't find the fee on the SMARTS site", "Reddit", "2026-01-01")
    kind, req = c.calls[0]
    assert kind == "beta" and req["model"] == "claude-opus-5-5"
    assert req["betas"] == ["server-side-fallback-2026-07-01"] and req["fallbacks"] == "default"
    schema = req["output_config"]["format"]["schema"]
    assert schema["properties"]["task"]["enum"] == cfg.task_names
    assert schema["properties"]["pain_point"]["enum"] == cfg.pain_point_names
    assert schema["additionalProperties"] is False and req["output_config"]["effort"] == "medium"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Source: Reddit" in req["messages"][0]["content"]
    assert "temperature" not in req and "thinking" not in req


def test_fallbacks_can_be_switched_off(tmp_path):
    c = FakeClient()
    t, _ = tagger(tmp_path, c, tagging={"fallbacks": "off"})
    t.tag("some post about GeoTracker")
    assert c.calls[0][0] == "messages" and "fallbacks" not in c.calls[0][1]


def test_prompt_lists_every_task_and_pain_point(tmp_path):
    cfg = cfg_for(tmp_path)
    p = system_prompt(cfg)
    assert all(n in p for n in cfg.task_names + cfg.pain_point_names)
    assert "Hard-to-read PDFs and forms" in p and "Reach" in p
    assert len(response_schema(cfg)["required"]) == 5


def test_second_call_for_the_same_post_uses_the_cache(tmp_path):
    c = FakeClient()
    t, _ = tagger(tmp_path, c)
    a = t.tag("same text")
    b = t.tag("same text")
    assert len(c.calls) == 1 and a.source == "api" and b.source == "cache" and a.task == b.task


def test_cache_survives_a_new_run_and_is_keyed_by_prompt_version(tmp_path):
    c = FakeClient()
    t, cfg = tagger(tmp_path, c)
    t.tag("hello GeoTracker")
    t.cache.save()
    c2 = FakeClient()
    t2 = Tagger(cfg, c2, TagCache(tmp_path / "cache.json"))
    assert t2.tag("hello GeoTracker").source == "cache" and not c2.calls
    cfg3 = cfg_for(tmp_path, tagging={"prompt_version": 2})
    assert cache_key("hello GeoTracker", cfg) != cache_key("hello GeoTracker", cfg3)


def test_retag_ignores_the_cache(tmp_path):
    c = FakeClient()
    t, _ = tagger(tmp_path, c)
    t.tag("x GeoTracker")
    t.tag("x GeoTracker", retag=True)
    assert len(c.calls) == 2


def test_a_refusal_becomes_unclassified_and_is_not_cached(tmp_path):
    c = FakeClient(response(stop_reason="refusal", text=""))
    t, _ = tagger(tmp_path, c)
    tag = t.tag("some post")
    assert tag.task == UNCLASSIFIED and t.usage.refusals == 1
    t.tag("some post")
    assert len(c.calls) == 2                      # tried again, because refusals are not cached


def test_an_api_error_does_not_stop_the_run_and_is_not_cached(tmp_path):
    c = FakeClient(raises=RuntimeError("boom"))
    t, _ = tagger(tmp_path, c)
    tag = t.tag("post")
    assert tag.task == UNCLASSIFIED and tag.source == "error" and t.usage.errors == 1
    assert t.cache.get(cache_key("post", t.cfg)) is None


def test_values_outside_the_lists_are_coerced_not_trusted(tmp_path):
    bad = good_tag(task="Make coffee", pain="Gremlins", conf="certain")
    t, _ = tagger(tmp_path, FakeClient(response(bad)))
    tag = t.tag("post")
    assert tag.task == UNCLASSIFIED and tag.pain_point == "None" and tag.confidence == "low"


def test_malformed_json_is_an_error_not_a_crash(tmp_path):
    t, _ = tagger(tmp_path, FakeClient(response(text="not json")))
    assert t.tag("post").source == "error"


def test_token_usage_and_cost_are_tracked(tmp_path):
    t, cfg = tagger(tmp_path, FakeClient())
    t.tag("a GeoTracker post")
    assert t.usage.calls == 1 and t.usage.input_tokens == 1200 and t.usage.cache_read == 900
    assert 0 < t.usage.cost(cfg) < 0.1


def test_very_long_posts_are_truncated_in_the_request(tmp_path):
    c = FakeClient()
    t, _ = tagger(tmp_path, c)
    t.tag("word " * 5000)
    assert "truncated for tagging" in c.calls[0][1]["messages"][0]["content"]


def test_estimate_counts_only_uncached_posts(tmp_path):
    c = FakeClient()
    t, _ = tagger(tmp_path, c)
    t.tag("one")
    est = t.estimate(["one", "two", "three"])
    assert est["cached"] == 1 and est["to_tag"] == 2 and not c.calls[1:]


def test_cache_file_is_valid_json(tmp_path):
    t, _ = tagger(tmp_path, FakeClient())
    t.tag("one")
    t.cache.save()
    assert isinstance(json.loads((tmp_path / "cache.json").read_text()), dict)
