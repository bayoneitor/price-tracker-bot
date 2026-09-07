import json

import pytest
import structlog

from price_tracker.observability.logging import bind_request_context, configure_logging


@pytest.fixture(autouse=True)
def reset_structlog():
    yield
    structlog.reset_defaults()


class TestConfigureLogging:
    def test_emits_json_lines(self, capsys):
        configure_logging(level="INFO")
        log = structlog.get_logger("test")
        log.info("hello", foo="bar")
        out = capsys.readouterr().out.strip().splitlines()
        assert out, "no log output captured"
        rec = json.loads(out[-1])
        assert rec["event"] == "hello"
        assert rec["foo"] == "bar"
        assert rec["level"] == "info"
        assert "timestamp" in rec

    def test_filters_below_level(self, capsys):
        configure_logging(level="WARNING")
        log = structlog.get_logger("test")
        log.info("ignored")
        log.warning("kept")
        out = capsys.readouterr().out.strip().splitlines()
        events = [json.loads(line)["event"] for line in out]
        assert "ignored" not in events
        assert "kept" in events

    def test_includes_iso_utc_timestamp(self, capsys):
        configure_logging(level="INFO")
        log = structlog.get_logger("test")
        log.info("when")
        rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rec["timestamp"].endswith("Z") or "+00:00" in rec["timestamp"]


class TestBindRequestContext:
    def test_context_appears_in_subsequent_logs(self, capsys):
        configure_logging(level="INFO")
        with bind_request_context(request_id="abc-123", scraper="amazon"):
            log = structlog.get_logger("test")
            log.info("scrape.start")
        rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rec["request_id"] == "abc-123"
        assert rec["scraper"] == "amazon"

    def test_context_is_cleared_after_block(self, capsys):
        configure_logging(level="INFO")
        with bind_request_context(request_id="abc"):
            pass
        log = structlog.get_logger("test")
        log.info("after")
        rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "request_id" not in rec


class TestStdlibLoggingBridge:
    """`logging.getLogger(__name__).info(...)` — used throughout `core.registry`
    (plugin discovery), the scrapers, and third-party libraries — reaches the
    same JSON stream `structlog.get_logger()` does. Without a handler on the
    stdlib root logger, every one of those calls is silently dropped: found
    while verifying that a discovered drop-in provider actually shows up in
    the startup log.
    """

    def test_a_plain_stdlib_logger_reaches_stdout_as_json(self, capsys):
        import logging

        configure_logging(level="INFO")
        logging.getLogger("price_tracker.core.registry").info("Registered thing: %s", "widget")
        out = capsys.readouterr().out.strip().splitlines()
        assert out, "no log output captured from the stdlib logger"
        rec = json.loads(out[-1])
        assert rec["event"] == "Registered thing: widget"
        assert rec["level"] == "info"
        assert "timestamp" in rec

    def test_a_stdlib_logger_is_also_filtered_by_level(self, capsys):
        import logging

        configure_logging(level="WARNING")
        logger = logging.getLogger("price_tracker.core.registry")
        logger.info("ignored")
        logger.warning("kept")
        out = capsys.readouterr().out.strip().splitlines()
        events = [json.loads(line)["event"] for line in out]
        assert "ignored" not in events
        assert "kept" in events
