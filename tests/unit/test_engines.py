"""Engine adapters, selection, and error normalisation."""

from __future__ import annotations

import json

import pytest

from bandwidth_logger.core.models import ErrorCategory, summarise_error
from bandwidth_logger.engines.base import classify_error_text
from bandwidth_logger.engines.ookla import OoklaEngine
from bandwidth_logger.engines.registry import (
    AUTOMATIC,
    available_engines,
    engine_by_name,
    known_engines,
    select_engine,
)
from bandwidth_logger.engines.speedtest_cli import SpeedtestCliEngine


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Cannot read: Temporary failure in name resolution", ErrorCategory.DNS_FAILURE),
            ("curl: (6) Could not resolve host: speedtest.net", ErrorCategory.DNS_FAILURE),
            ("connect: Network is unreachable", ErrorCategory.NO_NETWORK),
            ("No route to host", ErrorCategory.NO_NETWORK),
            ("NoServers: unable to find a test server", ErrorCategory.SERVER_UNAVAILABLE),
            ("Cannot retrieve speedtest configuration", ErrorCategory.SERVER_UNAVAILABLE),
            ("bind: Permission denied", ErrorCategory.PERMISSION_ERROR),
            ("The operation timed out after 30s", ErrorCategory.TIMEOUT),
            ("speedtest: command not found", ErrorCategory.ENGINE_MISSING),
        ],
    )
    def test_engine_wording_maps_to_a_stable_category(self, text, expected):
        assert classify_error_text(text) == expected

    def test_unrecognised_text_falls_back_to_process_error(self):
        assert classify_error_text("something nobody anticipated") == ErrorCategory.PROCESS_ERROR

    def test_empty_output_is_a_process_error(self):
        assert classify_error_text("") == ErrorCategory.PROCESS_ERROR
        assert classify_error_text("   \n ") == ErrorCategory.PROCESS_ERROR

    def test_a_specific_pattern_wins_over_a_general_one(self):
        # Contains both "timed out" and a resolution failure; the more precise
        # cause should be reported.
        text = "Temporary failure in name resolution; the request timed out"
        assert classify_error_text(text) == ErrorCategory.DNS_FAILURE


class TestErrorSummaries:
    def test_every_category_has_plain_wording_for_the_interface(self):
        for category in ErrorCategory:
            summary = summarise_error(category)
            assert summary
            assert summary != category.value
            # Short enough for a status line.
            assert len(summary) < 60

    def test_an_unknown_category_still_produces_wording(self):
        assert summarise_error("something_new")
        assert summarise_error(None)


class TestRegistry:
    def test_ookla_is_preferred_over_the_fallback(self):
        engines = known_engines()
        names = [engine.name for engine in engines]
        assert names.index("ookla") < names.index("speedtest-cli")

    def test_lookup_by_name(self):
        assert isinstance(engine_by_name("ookla"), OoklaEngine)
        assert isinstance(engine_by_name("speedtest-cli"), SpeedtestCliEngine)
        assert engine_by_name("nonexistent") is None

    def test_automatic_selection_picks_the_best_installed_engine(self, monkeypatch):
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: True)
        monkeypatch.setattr(SpeedtestCliEngine, "is_available", lambda self: True)

        assert select_engine(AUTOMATIC).name == "ookla"

    def test_automatic_selection_falls_back_when_ookla_is_absent(self, monkeypatch):
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: False)
        monkeypatch.setattr(SpeedtestCliEngine, "is_available", lambda self: True)

        assert select_engine(AUTOMATIC).name == "speedtest-cli"

    def test_no_engine_installed_selects_nothing(self, monkeypatch):
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: False)
        monkeypatch.setattr(SpeedtestCliEngine, "is_available", lambda self: False)

        assert select_engine(AUTOMATIC) is None
        assert available_engines() == []

    def test_a_deliberate_choice_is_not_silently_substituted(self, monkeypatch):
        """Asking for Ookla and getting speedtest-cli would quietly change
        what the numbers mean, so an explicit choice fails visibly instead.
        """
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: False)
        monkeypatch.setattr(SpeedtestCliEngine, "is_available", lambda self: True)

        assert select_engine("ookla") is None

    def test_an_explicit_choice_is_honoured_when_installed(self, monkeypatch):
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: True)
        monkeypatch.setattr(SpeedtestCliEngine, "is_available", lambda self: True)

        assert select_engine("speedtest-cli").name == "speedtest-cli"


class TestCommandConstruction:
    def test_ookla_requests_machine_readable_output(self, monkeypatch):
        engine = OoklaEngine()
        monkeypatch.setattr(engine, "executable_path", lambda: "/usr/bin/speedtest")

        command = engine.build_command()
        assert command[0] == "/usr/bin/speedtest"
        assert "--format=json" in command
        # Licence prompts would hang a scheduled run with no window attached.
        assert "--accept-license" in command
        assert "--accept-gdpr" in command

    def test_speedtest_cli_requests_json(self, monkeypatch):
        engine = SpeedtestCliEngine()
        monkeypatch.setattr(engine, "executable_path", lambda: "/usr/bin/speedtest-cli")

        command = engine.build_command()
        assert "--json" in command

    def test_a_missing_binary_raises_the_engine_missing_category(self, monkeypatch):
        from bandwidth_logger.engines.base import EngineError

        engine = OoklaEngine()
        monkeypatch.setattr(engine, "executable_path", lambda: None)

        with pytest.raises(EngineError) as caught:
            engine.build_command()
        assert caught.value.category == ErrorCategory.ENGINE_MISSING

    def test_commands_are_argument_vectors_never_shell_strings(self, monkeypatch):
        """Nothing is ever handed to a shell, so nothing can be injected."""
        for engine_class, path in (
            (OoklaEngine, "/usr/bin/speedtest"),
            (SpeedtestCliEngine, "/usr/bin/speedtest-cli"),
        ):
            engine = engine_class()
            monkeypatch.setattr(engine, "executable_path", lambda path=path: path)
            command = engine.build_command()

            assert isinstance(command, list)
            assert all(isinstance(part, str) for part in command)
            assert not any(character in part for part in command for character in ";|&$`")


class TestInstallGuidance:
    def test_ookla_guidance_states_it_is_not_bundled(self):
        guidance = OoklaEngine().install_guidance()

        assert guidance.bundled is False
        assert "Ookla" in guidance.detail
        assert "licence does not allow" in guidance.detail
        assert guidance.commands
        assert guidance.url

    def test_every_engine_offers_guidance(self):
        for engine in known_engines():
            guidance = engine.install_guidance()
            assert guidance.summary
            assert guidance.detail


class TestOoklaIdentityCheck:
    def test_a_speedtest_that_is_not_ooklas_is_not_accepted(self, monkeypatch):
        """``speedtest`` is sometimes a symlink to the unrelated speedtest-cli.

        Running that with ``--format=json`` would fail confusingly, so the
        binary's identity is confirmed rather than assumed.
        """
        import subprocess

        engine = OoklaEngine()
        monkeypatch.setattr(engine, "executable_path", lambda: "/usr/bin/speedtest")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "speedtest-cli 2.1.3", ""),
        )

        assert engine.is_available() is False

    def test_the_real_ookla_banner_is_accepted(self, monkeypatch):
        import subprocess

        engine = OoklaEngine()
        monkeypatch.setattr(engine, "executable_path", lambda: "/usr/bin/speedtest")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a, 0, "Speedtest by Ookla 1.2.0.84 (ea6b6773cf)", ""
            ),
        )

        assert engine.is_available() is True


class TestFailureClassificationOnEngines:
    def test_the_full_message_is_preserved_alongside_the_category(self):
        engine = OoklaEngine()
        detail = "[error] Configuration - Cannot read: Temporary failure in name resolution"

        category, message = engine.classify_failure(2, "", detail)

        assert category == ErrorCategory.DNS_FAILURE
        assert message == detail

    def test_a_silent_failure_still_produces_a_message(self):
        category, message = OoklaEngine().classify_failure(1, "", "")

        assert category == ErrorCategory.PROCESS_ERROR
        assert "status 1" in message


class TestParsedOutputRoutesThroughTheEngine:
    def test_ookla_engine_parses_its_own_format(self, monkeypatch):
        from support.fake_engine import SUCCESS_RESULT

        engine = OoklaEngine()
        monkeypatch.setattr(engine, "version", lambda: "1.2.0")

        measurement = engine.parse_output(json.dumps(SUCCESS_RESULT))
        assert measurement.engine_name == "ookla"
        assert measurement.download_bps == 95_000_000
