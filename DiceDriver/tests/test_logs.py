"""Logging: the durable tier, and the guarantee that a stuck console cannot silence the file log."""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import pytest

from dicedriver import config, logs


def _cfg(tmp_path, **log):
    base = {
        "file": str(tmp_path / "main.log"),
        "journal_file": str(tmp_path / "journal.log"),
        "stderr_file": str(tmp_path / "stderr.log"),
        "console": False,
    }
    base.update(log)
    return config.from_dict({"log": base}, tmp_path)


@pytest.fixture(autouse=True)
def clean():
    yield
    logs.shutdown()


def test_journal_is_written_before_anything_drains_a_queue(tmp_path):
    logs.setup(_cfg(tmp_path))
    logging.getLogger("dd.test").warning("durable line")
    # no shutdown, no listener join: the record has to be on disk already
    assert "durable line" in (tmp_path / "journal.log").read_text(encoding="utf-8")


def test_journal_keeps_the_lifecycle_lines(tmp_path):
    logs.setup(_cfg(tmp_path, level="trace"))
    logging.getLogger("dd.main").info("DiceDriver starting")
    assert "DiceDriver starting" in (tmp_path / "journal.log").read_text(encoding="utf-8")


def test_a_stuck_console_does_not_stall_the_file_log(tmp_path):
    gate = threading.Event()

    class Stuck:
        encoding = "utf-8"

        def write(self, text):
            gate.wait(10)
            return len(text)

        def flush(self):
            pass

        def isatty(self):
            return False

    real = sys.stderr
    sys.stderr = Stuck()
    try:
        logs.setup(_cfg(tmp_path, console=True))
        logging.getLogger("dd.test").error("must still reach the file")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "must still reach the file" in (tmp_path / "main.log").read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        assert "must still reach the file" in (tmp_path / "main.log").read_text(encoding="utf-8")
        # the copy in front of the console got it too: the file write happens first
        assert "must still reach the file" in (tmp_path / "stderr.log").read_text(encoding="utf-8")
    finally:
        gate.set()
        logs.shutdown()
        sys.stderr = real


def test_tee_keeps_recording_when_the_stream_itself_fails(tmp_path):
    class Broken:
        def write(self, text):
            raise OSError("the console is gone")

        def flush(self):
            raise OSError("the console is gone")

    tee = logs._Tee(tmp_path / "copy.log", Broken())
    assert tee.write("still recorded") == len("still recorded")
    tee.flush()
    tee.close()
    assert "still recorded" in (tmp_path / "copy.log").read_text(encoding="utf-8")


def test_shutdown_flushes_restores_the_stream_and_detaches(tmp_path):
    real = sys.stderr
    logs.setup(_cfg(tmp_path))
    assert isinstance(sys.stderr, logs._Tee)
    logging.getLogger("dd.test").warning("tail line")
    sys.stderr.write("raw stderr text\n")  # what a traceback or a native abort message does
    logs.shutdown()
    assert sys.stderr is real
    assert "tail line" in (tmp_path / "main.log").read_text(encoding="utf-8")
    assert "raw stderr text" in (tmp_path / "stderr.log").read_text(encoding="utf-8")


def test_queue_depth_reports_each_tier(tmp_path):
    logs.setup(_cfg(tmp_path, console=True))
    assert set(logs.queue_depth()) == {"file", "console"}
