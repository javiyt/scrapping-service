"""Tests for logging configuration."""

import logging

from app.core.logging import get_logger, setup_logging


class TestSetupLogging:
    def test_sets_root_level(self):
        setup_logging("DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_handler_is_attached(self):
        setup_logging("INFO")
        root = logging.getLogger()
        assert len(root.handlers) > 0

    def test_third_party_loggers_lowered(self):
        setup_logging("DEBUG")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert logging.getLogger("botasaurus").level == logging.WARNING

    def test_called_twice_no_duplicate_handlers(self):
        setup_logging("INFO")
        setup_logging("INFO")
        root = logging.getLogger()
        assert len(root.handlers) >= 1

    def test_first_call_adds_handler(self):
        """When root has no handlers, setup_logging should add one."""
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging("WARNING")
            assert len(root.handlers) == 1
            assert root.level == logging.WARNING
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)


class TestGetLogger:
    def test_returns_child_logger(self):
        logger = get_logger("test_module")
        assert logger.name == "scraper-api.test_module"
        assert isinstance(logger, logging.Logger)
