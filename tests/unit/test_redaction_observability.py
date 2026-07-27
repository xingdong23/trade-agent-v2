from trade_agent.adapters.observability import StructuredTracer, TraceEvent
from trade_agent.core.security import Redactor


class CaptureExporter:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def export(self, event: TraceEvent) -> None:
        self.events.append(event)


def test_redactor_removes_nested_secrets_and_configured_fields() -> None:
    redacted = Redactor(("tax_id",)).redact(
        {
            "api_key": "sk-1234567890",
            "headers": {"authorization": "Bearer abc.def.ghi"},
            "tax_id": "sensitive",
            "message": "provider key-secretvalue failed",
        }
    )
    assert redacted == {
        "api_key": "[REDACTED]",
        "headers": {"authorization": "[REDACTED]"},
        "tax_id": "[REDACTED]",
        "message": "provider [REDACTED] failed",
    }


def test_structured_trace_exports_only_sanitized_attributes() -> None:
    exporter = CaptureExporter()
    tracer = StructuredTracer(exporter=exporter)
    event = tracer.emit(
        correlation_id="corr-1",
        event_type="llm.completed",
        outcome="success",
        attributes={
            "graph": "supervisor",
            "node": "classify",
            "llm_route": "intent_classifier",
            "prompt_version": "intent.v1",
            "tokens": 42,
            "latency_ms": 120,
            "retry": 1,
            "approval": "not_required",
            "token": "secret-value",
        },
    )
    assert event.attributes["token"] == "[REDACTED]"
    assert exporter.events == [event]
    assert tracer.events("corr-1") == (event,)
