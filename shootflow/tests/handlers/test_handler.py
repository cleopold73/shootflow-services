# Python
import json
import types
import sys
import pytest

import shootflow.handlers.handler as gh  # the generic handler module


def _register_dummy_module(monkeypatch, name="dummy.targets"):
    """
    Create and register a dummy module in sys.modules with a set of target callables.
    Returns (module_name, funcs_dict).
    """
    mod = types.ModuleType(name)
    calls = []

    def passthrough_target(event, context):
        calls.append(("passthrough", event))
        return {"ok": True, "seen": event}

    def sqs_target(event, context):
        calls.append(("sqs", event))
        # raise when a special flag is present to simulate failure
        if isinstance(event, dict) and event.get("__fail__"):
            raise RuntimeError("boom")
        return {"done": True}

    def s3_like_target(event, context):
        calls.append(("s3", event))
        # treat as S3 event; fail on a specific key if present
        try:
            rec = (event or {}).get("Records", [])[0]
            key = rec.get("s3", {}).get("object", {}).get("key")
            if key == "bad-key.jpg":
                raise RuntimeError("ingest fail")
        except Exception:
            # any parsing issue -> raise
            raise
        return {"ingested": True}

    def sns_target(event, context):
        calls.append(("sns", event))
        if isinstance(event, dict) and event.get("__fail__"):
            raise RuntimeError("sns-fail")
        return {"ok": True}

    mod.calls = calls
    mod.passthrough_target = passthrough_target
    mod.sqs_target = sqs_target
    mod.s3_like_target = s3_like_target
    mod.sns_target = sns_target

    monkeypatch.setitem(sys.modules, name, mod)
    return name, mod


def _sqs_event(bodies_with_ids):
    """
    bodies_with_ids: list of tuples (message_id, body_object_or_string)
    Body is JSON-encoded if it's a dict; passed through if already a string.
    """
    records = []
    for mid, body in bodies_with_ids:
        body_str = json.dumps(body) if isinstance(body, (dict, list)) else body
        records.append({"messageId": mid, "body": body_str})
    return {"Records": records}


def _sns_event(messages):
    """
    messages: list of tuples (message_id, message_payload_dict_or_string)
    """
    records = []
    for mid, msg in messages:
        msg_str = json.dumps(msg) if isinstance(msg, (dict, list)) else msg
        records.append(
            {
                "EventSource": "aws:sns",
                "Sns": {
                    "MessageId": mid,
                    "Message": msg_str,
                },
            }
        )
    return {"Records": records}


def _s3_event(records):
    """
    records: list of dicts with minimal S3 event structure.
    """
    return {"Records": records}


def test_passthrough_adapter_calls_target_and_returns(monkeypatch):
    mod_name, mod = _register_dummy_module(monkeypatch)
    monkeypatch.setenv("LAMBDA_TARGET", f"{mod_name}:passthrough_target")
    monkeypatch.delenv("LAMBDA_ADAPTER", raising=False)  # default = passthrough

    event = {"hello": "world"}
    res = gh.handler(event, None)

    assert res == {"ok": True, "seen": event}
    assert mod.calls and mod.calls[-1][0] == "passthrough"
    assert mod.calls[-1][1] == event


def test_missing_target_env_raises(monkeypatch):
    monkeypatch.delenv("LAMBDA_TARGET", raising=False)
    monkeypatch.delenv("LAMBDA_ADAPTER", raising=False)
    with pytest.raises(RuntimeError) as e:
        gh.handler({}, None)
    assert "LAMBDA_TARGET" in str(e.value)


def test_invalid_adapter_raises(monkeypatch):
    mod_name, _ = _register_dummy_module(monkeypatch)
    monkeypatch.setenv("LAMBDA_TARGET", f"{mod_name}:passthrough_target")
    monkeypatch.setenv("LAMBDA_ADAPTER", "nope")
    with pytest.raises(RuntimeError) as e:
        gh.handler({}, None)
    assert "Unsupported LAMBDA_ADAPTER" in str(e.value)


def test_sqs_adapter_partial_batch_failures(monkeypatch):
    mod_name, mod = _register_dummy_module(monkeypatch)
    monkeypatch.setenv("LAMBDA_TARGET", f"{mod_name}:sqs_target")
    monkeypatch.setenv("LAMBDA_ADAPTER", "sqs")

    # two messages: one ok, one failing
    evt = _sqs_event(
        [
            ("m1", {"value": 1}),
            ("m2", {"__fail__": True}),
        ]
    )

    res = gh.handler(evt, None)

    assert res == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    # both messages should have been passed to the target
    assert [c[0] for c in mod.calls] == ["sqs", "sqs"]


def test_sqs_adapter_handles_double_encoded_json(monkeypatch):
    mod_name, mod = _register_dummy_module(monkeypatch)
    monkeypatch.setenv("LAMBDA_TARGET", f"{mod_name}:sqs_target")
    monkeypatch.setenv("LAMBDA_ADAPTER", "sqs")

    # Body is a JSON string containing a JSON string (double-encoded)
    double_encoded = json.dumps(json.dumps({"value": 123}))
    evt = _sqs_event([("m1", double_encoded)])

    res = gh.handler(evt, None)
    assert res == {"batchItemFailures": []}
    # Target should receive the decoded dict
    assert mod.calls and mod.calls[-1][1] == {"value": 123}


def test_sqs_s3_adapter_success_and_failure(monkeypatch):
    mod_name, mod = _register_dummy_module(monkeypatch)
    monkeypatch.setenv("LAMBDA_TARGET", f"{mod_name}:s3_like_target")
    monkeypatch.setenv("LAMBDA_ADAPTER", "sqs_s3")

    # S3 event in body with two records: one good, one bad -> the message fails
    s3_body = _s3_event(
        [
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "ok.jpg"}}},
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "bad-key.jpg"}}},
        ]
    )
    evt = _sqs_event([("m1", s3_body)])

    res = gh.handler(evt, None)

    assert res == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    # Target should be called once per S3 record
    assert [c[0] for c in mod.calls] == ["s3", "s3"]


def test_sqs_s3_adapter_ignores_messages_without_s3_records(monkeypatch):
    mod_name, mod = _register_dummy_module(monkeypatch)
    monkeypatch.setenv("LAMBDA_TARGET", f"{mod_name}:s3_like_target")
    monkeypatch.setenv("LAMBDA_ADAPTER", "sqs_s3")

    # Message body has no S3 Records; should be acknowledged (no failure)
    evt = _sqs_event([("m1", {"foo": "bar"})])

    res = gh.handler(evt, None)
    assert res == {"batchItemFailures": []}
    assert mod.calls == []  # nothing invoked


def test_sns_adapter_aggregates_counts(monkeypatch):
    mod_name, mod = _register_dummy_module(monkeypatch)
    monkeypatch.setenv("LAMBDA_TARGET", f"{mod_name}:sns_target")
    monkeypatch.setenv("LAMBDA_ADAPTER", "sns")

    evt = _sns_event(
        [
            ("s1", {"hello": "world"}),
            ("s2", {"__fail__": True}),
        ]
    )

    res = gh.handler(evt, None)

    assert res == {"processed": 1, "errors": 1}
    # Target should be called for both
    assert [c[0] for c in mod.calls] == ["sns", "sns"]


def test_load_target_errors(monkeypatch):
    # Unknown module
    monkeypatch.setenv("LAMBDA_TARGET", "no.such.module:fn")
    monkeypatch.setenv("LAMBDA_ADAPTER", "passthrough")
    with pytest.raises(ModuleNotFoundError):
        gh.handler({}, None)

    # Known module, missing function
    mod_name, _ = _register_dummy_module(monkeypatch, name="dummy.targets2")
    monkeypatch.setenv("LAMBDA_TARGET", f"{mod_name}:missing_fn")
    with pytest.raises(ValueError) as e:
        gh.handler({}, None)
    assert "not found" in str(e.value)
