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
    def test_ookla_is_the_only_supported_engine(self):
        """The speedtest-cli fallback was removed in 1.1.0.

        It under-reported throughput on fast connections, and its Ubuntu
        package claims the same /usr/bin/speedtest path Ookla installs to,
        so its presence blocked the accurate engine entirely.
        """
        assert [engine.name for engine in known_engines()] == ["ookla"]

    def test_lookup_by_name(self):
        assert isinstance(engine_by_name("ookla"), OoklaEngine)
        assert engine_by_name("speedtest-cli") is None
        assert engine_by_name("nonexistent") is None

    def test_automatic_selection_picks_ookla_when_installed(self, monkeypatch):
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: True)

        assert select_engine(AUTOMATIC).name == "ookla"

    def test_no_engine_installed_selects_nothing(self, monkeypatch):
        """Nothing is substituted. The interface reports that no engine is
        available rather than measuring with something less accurate.
        """
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: False)

        assert select_engine(AUTOMATIC) is None
        assert available_engines() == []

    def test_an_explicit_choice_is_honoured_when_installed(self, monkeypatch):
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: True)

        assert select_engine("ookla").name == "ookla"

    def test_an_explicit_choice_that_is_absent_fails_visibly(self, monkeypatch):
        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: False)

        assert select_engine("ookla") is None


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

    def test_a_missing_binary_raises_the_engine_missing_category(self, monkeypatch):
        from bandwidth_logger.engines.base import EngineError

        engine = OoklaEngine()
        monkeypatch.setattr(engine, "executable_path", lambda: None)

        with pytest.raises(EngineError) as caught:
            engine.build_command()
        assert caught.value.category == ErrorCategory.ENGINE_MISSING

    def test_commands_are_argument_vectors_never_shell_strings(self, monkeypatch):
        """Nothing is ever handed to a shell, so nothing can be injected."""
        for engine_class, path in ((OoklaEngine, "/usr/bin/speedtest"),):
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

    def test_the_install_commands_work_on_ubuntu_24_04(self):
        """Both details here were found by the commands failing in real use.

        Ookla publishes no repository for Ubuntu 24.04 ("noble"), so the
        upstream script must be pointed at the jammy one. And Ubuntu's
        speedtest-cli package owns /usr/bin/speedtest, so dpkg refuses to
        unpack Ookla's package while it is installed.
        """
        commands = OoklaEngine().install_guidance().commands
        joined = "\n".join(commands)

        assert "dist=jammy" in joined, "the unmodified script fails on 24.04"
        assert "remove" in joined and "speedtest-cli" in joined, (
            "the conflicting package must be removed first"
        )
        # Order matters: the removal has to precede the install.
        assert commands.index("sudo apt-get remove -y speedtest-cli") == 0

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


class TestPinnedServer:
    """Pinning a test server.

    Added in 1.2.0 after a gigabit line measured 696 Mbps against the
    lowest-latency server and 926 against another one 0.5 ms further away.
    The engine chooses by latency, which is not the same as fastest, and it
    re-chooses on every run -- so an unpinned history records server changes
    as if they were connection changes.
    """

    def test_no_server_argument_when_unpinned(self, monkeypatch):
        engine = OoklaEngine()
        monkeypatch.setattr(engine, "executable_path", lambda: "/usr/bin/speedtest")

        assert not any("--server-id" in part for part in engine.build_command())

    def test_the_pinned_server_is_passed_to_the_engine(self, monkeypatch):
        engine = OoklaEngine()
        engine.server_id = "16976"
        monkeypatch.setattr(engine, "executable_path", lambda: "/usr/bin/speedtest")

        assert "--server-id=16976" in engine.build_command()

    @pytest.mark.parametrize("bad", ["; rm -rf /", "16976; ls", "abc", "--help", " "])
    def test_a_non_numeric_server_id_is_refused(self, monkeypatch, bad):
        """The value comes from a stored setting, so it is validated.

        The command is an argument vector and is never shell-interpreted, so
        this is not an injection fix; it turns a confusing engine error into a
        clear one at the point the mistake is visible.
        """
        from bandwidth_logger.engines.base import EngineError

        engine = OoklaEngine()
        engine.server_id = bad
        monkeypatch.setattr(engine, "executable_path", lambda: "/usr/bin/speedtest")

        with pytest.raises(EngineError):
            engine.build_command()


class TestServerList:
    def test_the_server_list_is_parsed(self):
        from bandwidth_logger.core.result_parser import describe_server, parse_server_list

        payload = json.dumps(
            {
                "type": "serverList",
                "servers": [
                    {"id": 16976, "name": "Spectrum", "location": "New York, NY",
                     "country": "United States"},
                    {"id": 13098, "name": "Pilot Fiber", "location": "New York, NY",
                     "country": "United States"},
                ],
            }
        )
        servers = parse_server_list(payload)

        assert [s["id"] for s in servers] == ["16976", "13098"]
        assert servers[0]["name"] == "Spectrum"
        assert describe_server(servers[0]) == "Spectrum - New York, NY (16976)"

    def test_unusable_entries_are_skipped_not_fatal(self):
        """A partly usable list beats no list."""
        from bandwidth_logger.core.result_parser import parse_server_list

        payload = json.dumps(
            {
                "servers": [
                    {"id": 16976, "name": "Spectrum", "location": "New York, NY"},
                    {"name": "No id at all"},
                    "not even an object",
                    {"id": "not-a-number", "name": "Bad id"},
                ]
            }
        )
        servers = parse_server_list(payload)

        assert [s["id"] for s in servers] == ["16976"]

    def test_a_response_without_servers_is_rejected(self):
        from bandwidth_logger.core.result_parser import ParseError, parse_server_list

        with pytest.raises(ParseError):
            parse_server_list(json.dumps({"type": "result"}))

    def test_a_server_with_no_location_still_describes(self):
        from bandwidth_logger.core.result_parser import describe_server

        assert describe_server({"id": "1", "name": "Somewhere", "location": "", "country": ""}) == (
            "Somewhere (1)"
        )
