"""M5/M6/M9: AdapterInterface + FakeTransport + KimiAdapter (synthetic fixtures)
+ net-new provider stubs + probe() fail-closed."""

import json

import pytest

from gce_ingestion.adapters.base import AdapterNotImplementedError
from gce_ingestion.adapters.chatgpt import ChatGPTAdapter
from gce_ingestion.adapters.claude import ClaudeAdapter
from gce_ingestion.adapters.kimi import KimiAdapter
from gce_ingestion.adapters.netnew import NetNewProviderStub
from gce_ingestion.failures import FailureCode
from gce_ingestion.transport.fake import FakeTransport


def _load_conversations(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_fixture_total_is_small_and_not_93_or_106(synthetic_kimi_fixture):
    data = _load_conversations(synthetic_kimi_fixture)
    n = len(data["conversations"])
    assert n <= 10
    assert n not in (93, 106)  # C4: never a count-based golden baseline


def test_kimi_adapter_probe_and_roundtrip_against_synthetic(synthetic_kimi_fixture):
    data = _load_conversations(synthetic_kimi_fixture)
    transport = FakeTransport(conversations=data["conversations"])
    adapter = KimiAdapter(transport)
    probe = adapter.probe()
    assert probe.structure_ok is True
    # enumerate sees the fixture conversation ids only (no invented count)
    ids = adapter.enumerate("acc").conversation_ids
    assert set(ids) == set(data["conversations"].keys())
    # traverse captures faithful bytes (dom-snapshot)
    cap = adapter.traverse_and_capture(next(iter(data["conversations"])))
    assert cap.structure_ok and cap.payload and cap.capture_path == "dom-snapshot"
    assert cap.failure_code is None


def test_kimi_probe_fail_closed_on_empty_dom():
    transport = FakeTransport(conversations={})
    adapter = KimiAdapter(transport)
    probe = adapter.probe()
    assert probe.structure_ok is False
    assert probe.signal == "F2_STOP"
    cap = adapter.traverse_and_capture("missing")
    assert cap.structure_ok is False
    assert cap.failure_code == FailureCode.F2.value


def test_netnew_stubs_isolated_and_not_implemented():
    # ChatGPT is no longer a net-new stub: it is implemented against the live
    # first-party surface (Stage 1 recon 2026-09-26). Its own contract is covered by
    # tests/test_chatgpt_adapter.py. The A8 isolation guarantee still holds for every
    # provider that remains un-enabled -- Claude below.
    for adapter_cls in (ClaudeAdapter,):
        a = adapter_cls(FakeTransport(conversations={}))
        probe = a.probe()
        assert probe.structure_ok is False
        assert probe.signal == "NET_NEW_PROVIDER"
        with pytest.raises(AdapterNotImplementedError):
            a.enumerate("acc")
        with pytest.raises(AdapterNotImplementedError):
            a.traverse_and_capture("c1")


def test_chatgpt_adapter_is_implemented_not_a_stub():
    adapter = ChatGPTAdapter(FakeTransport(conversations={}))
    assert adapter.provider == "chatgpt"
    assert not isinstance(adapter, NetNewProviderStub)
