import os
import pty
import select
import sys
import threading
import time

import pytest

from graybox.cli import (
    Key,
    _combine_surrogate_pair,
    _decode_key,
    _is_high_surrogate,
    _is_low_surrogate,
    _utf8_sequence_len,
)


def _drive_interactive_input(setup, timeout=10):
    """Run the real _interactive_input() against one end of a pty, with
    `setup(master)` responsible for writing/closing the other end however
    a given test needs. Returns (value, error_message_or_None, still_alive).
    """
    from graybox.cli import _interactive_input

    master, slave = pty.openpty()
    stop = threading.Event()
    ready = threading.Event()
    holder: dict[str, object] = {}

    def drain() -> None:
        # Act like a terminal emulator: consume the application's output.
        # On macOS, TCSADRAIN can block until the master reads that output.
        output = bytearray()
        while not stop.is_set():
            try:
                readable, _, _ = select.select([master], [], [], 0.05)
                if not readable:
                    continue
                chunk = os.read(master, 4096)
            except (OSError, ValueError):
                return  # setup may close the master to exercise EOF
            if not chunk:
                return
            if not ready.is_set():
                output.extend(chunk)
                if b"prompt> " in output:
                    ready.set()

    drainer = threading.Thread(target=drain)
    drainer.start()

    def typer():
        if not ready.wait(timeout):
            holder["error"] = "interactive input never displayed its prompt"
            return
        try:
            setup(master)
        except Exception as exc:
            holder["error"] = f"{type(exc).__name__}: {exc}"

    typer_thread = threading.Thread(target=typer, daemon=True)
    typer_thread.start()

    def run_input() -> None:
        stdin_wrapper = os.fdopen(os.dup(slave), "rb", buffering=0)
        stdout_wrapper = os.fdopen(os.dup(slave), "w", buffering=1)
        saved_in, saved_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = stdin_wrapper, stdout_wrapper
        try:
            holder["val"] = _interactive_input("prompt> ")
        except Exception as exc:  # pragma: no cover
            holder["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            sys.stdin, sys.stdout = saved_in, saved_out
            for wrapper in (stdin_wrapper, stdout_wrapper):
                try:
                    wrapper.close()
                except OSError:
                    pass

    input_thread = threading.Thread(target=run_input)
    input_thread.start()
    input_thread.join(timeout)
    alive = input_thread.is_alive()

    stop.set()
    drainer.join()
    for fd_obj in (master, slave):
        try:
            os.close(fd_obj)
        except OSError:
            pass
    # Closing the master releases a blocked reader. Wait for its finally
    # block to restore sys.stdin/stdout before another test can start.
    input_thread.join(timeout)
    typer_thread.join(timeout)

    return holder.get("val"), holder.get("error"), alive


class TestUtf8SequenceLen:
    def test_ascii_single_byte(self):
        assert _utf8_sequence_len(ord("a")) == 1

    def test_two_byte_lead(self):
        assert _utf8_sequence_len(0xC3) == 2

    def test_three_byte_lead(self):
        assert _utf8_sequence_len(0xE4) == 3

    def test_four_byte_lead(self):
        assert _utf8_sequence_len(0xF0) == 4

    def test_continuation_byte_falls_back_to_one(self):
        assert _utf8_sequence_len(0xA1) == 1


class TestDecodeKey:
    def test_ascii(self):
        assert _decode_key(b"a") == "a"

    def test_accented_two_byte_char(self):
        assert _decode_key("á".encode("utf-8")) == "á"

    def test_three_byte_char(self):
        assert _decode_key("中".encode("utf-8")) == "中"

    def test_four_byte_char(self):
        assert _decode_key("🪴".encode("utf-8")) == "🪴"

    def test_incomplete_sequence_returns_empty(self):
        assert _decode_key(b"\xc3") == ""

    def test_invalid_byte_returns_empty(self):
        assert _decode_key(b"\xff") == ""


class TestInteractiveInputUtf8:
    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_accented_input_preserved_through_getch(self):
        master, slave = pty.openpty()
        collected: list[bytes] = []
        stop = threading.Event()

        def drain() -> None:
            while not stop.is_set():
                r, _, _ = select.select([master], [], [], 0.05)
                if not r:
                    continue
                try:
                    d = os.read(master, 4096)
                except (OSError, ValueError):
                    return
                if not d:
                    return
                collected.append(d)

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()

        def typer() -> None:
            deadline = time.time() + 10
            while time.time() < deadline:
                if b"prompt>" in b"".join(collected):
                    break
                time.sleep(0.05)
            os.write(master, "água para as plantas".encode("utf-8"))
            time.sleep(0.2)
            os.write(master, b"\r")

        typer_thread = threading.Thread(target=typer, daemon=True)
        typer_thread.start()

        holder: dict[str, str] = {}
        import io

        def run_input() -> None:
            import sys as _sys

            from graybox.cli import _interactive_input

            stdin_wrapper = os.fdopen(os.dup(slave), "rb", buffering=0)
            stdout_wrapper = os.fdopen(os.dup(slave), "w", buffering=1)
            saved_in, saved_out = _sys.stdin, _sys.stdout
            _sys.stdin, _sys.stdout = stdin_wrapper, stdout_wrapper
            try:
                holder["val"] = _interactive_input("prompt> ")
            except Exception as exc:  # pragma: no cover
                holder["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                _sys.stdin, _sys.stdout = saved_in, saved_out
                stdin_wrapper.close()
                stdout_wrapper.close()

        input_thread = threading.Thread(target=run_input)
        input_thread.start()
        input_thread.join(15)
        stop.set()

        try:
            os.close(master)
        finally:
            os.close(slave)

        assert not input_thread.is_alive(), "interactive input hung"
        assert "error" not in holder, holder.get("error")
        assert holder["val"] == "água para as plantas"


class TestByteDelayedUtf8:
    """The lead byte and continuation byte(s) of one UTF-8 character can
    legitimately arrive in SEPARATE reads, a few/tens of ms apart - real
    SSH/tmux/system-load jitter, not a corrupted stream. A correct
    implementation must reassemble these rather than treating a timeout
    on the continuation byte as "drop the character"."""

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_accented_char_survives_realistic_keystroke_latency(self):
        def setup(master):
            time.sleep(0.1)
            char = "é".encode("utf-8")  # 0xC3 0xA9
            os.write(master, char[:1])
            time.sleep(0.03)  # gap comfortably past a short poll window
            os.write(master, char[1:])
            time.sleep(0.1)
            os.write(master, b"\r")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive, "interactive input hung"
        assert err is None, err
        assert val == "é", f"expected 'é', got {val!r} (character was dropped)"

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_three_byte_char_survives_200ms_gap(self):
        def setup(master):
            time.sleep(0.1)
            char = "中".encode("utf-8")
            os.write(master, char[:1])
            time.sleep(0.2)
            os.write(master, char[1:])
            time.sleep(0.1)
            os.write(master, b"\r")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive
        assert err is None, err
        assert val == "中"

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_four_byte_emoji_split_across_three_delayed_writes(self):
        def setup(master):
            time.sleep(0.1)
            char = "🪴".encode("utf-8")
            os.write(master, char[:1])
            time.sleep(0.03)
            os.write(master, char[1:2])
            time.sleep(0.03)
            os.write(master, char[2:])
            time.sleep(0.1)
            os.write(master, b"\r")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive
        assert err is None, err
        assert val == "🪴"

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_delayed_char_interleaved_with_normal_ascii_typing(self):
        def setup(master):
            time.sleep(0.1)
            os.write(master, b"a")
            time.sleep(0.01)
            os.write(master, b"b")
            time.sleep(0.01)
            char = "é".encode("utf-8")
            os.write(master, char[:1])
            time.sleep(0.03)
            os.write(master, char[1:])
            time.sleep(0.01)
            os.write(master, b"c")
            time.sleep(0.1)
            os.write(master, b"\r")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive
        assert err is None, err
        assert val == "abéc"


class TestGraphemeAwareEditing:
    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_backspace_removes_whole_combining_grapheme(self):
        # "e" + combining acute accent + "X": one backspace should remove
        # the combined e+accent as a single unit, not just the accent.
        def setup(master):
            time.sleep(0.1)
            os.write(master, "e\u0301X".encode("utf-8"))
            time.sleep(0.05)
            os.write(master, b"\x7f")
            time.sleep(0.05)
            os.write(master, b"\r")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive
        assert err is None, err
        assert val == "e\u0301"

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_cursor_navigation_mid_line_insert(self):
        def setup(master):
            time.sleep(0.1)
            os.write(master, b"helo")
            time.sleep(0.02)
            os.write(master, b"\x1b[D")  # Left
            time.sleep(0.02)
            os.write(master, b"\x1b[D")  # Left
            time.sleep(0.02)
            os.write(master, b"l")
            time.sleep(0.05)
            os.write(master, b"\r")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive
        assert err is None, err
        assert val == "hello"

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_home_end_and_delete(self):
        def setup(master):
            time.sleep(0.1)
            os.write(master, b"xhello")
            time.sleep(0.02)
            os.write(master, b"\x1b[H")  # Home
            time.sleep(0.02)
            os.write(master, b"\x1b[3~")  # Delete
            time.sleep(0.05)
            os.write(master, b"\r")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive
        assert err is None, err
        assert val == "hello"

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_plain_escape_still_cancels(self):
        def setup(master):
            time.sleep(0.1)
            os.write(master, b"abc")
            time.sleep(0.05)
            os.write(master, b"\x1b")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive
        assert err is None, err
        assert val is None


class TestEofHandling:
    """A closed/broken input stream must cancel cleanly (like Esc) - never
    crash with an uncaught exception, and never spin the read loop forever."""

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_eof_mid_multibyte_sequence(self):
        # Send only the lead byte of a 3-byte character, then close the
        # stream entirely before any continuation byte arrives.
        def setup(master):
            time.sleep(0.1)
            os.write(master, "中".encode("utf-8")[:1])
            time.sleep(0.05)
            os.close(master)

        val, err, alive = _drive_interactive_input(setup)
        assert not alive, "interactive input hung on EOF mid-sequence"
        assert err is None, err
        assert val is None

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_eof_with_nothing_typed(self):
        def setup(master):
            time.sleep(0.1)
            os.close(master)

        val, err, alive = _drive_interactive_input(setup)
        assert not alive, "interactive input hung on immediate EOF"
        assert err is None, err
        assert val is None

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_eof_after_partial_typing_preserves_nothing_but_does_not_crash(self):
        def setup(master):
            time.sleep(0.1)
            os.write(master, b"hello")
            time.sleep(0.05)
            os.close(master)

        val, err, alive = _drive_interactive_input(setup)
        assert not alive, "interactive input hung on EOF after typing"
        assert err is None, err


class TestNonTtyFallback:
    """Piped/redirected stdin (not a real tty) must not crash on
    termios.tcgetattr - it should fall back to plain line reading."""

    def test_piped_text_is_read_via_line_mode(self):
        from graybox.cli import _interactive_input

        r, w = os.pipe()
        os.write(w, b"plain piped text\n")
        os.close(w)
        saved_in = sys.stdin
        sys.stdin = os.fdopen(r, "r")
        try:
            val = _interactive_input("prompt> ")
        finally:
            sys.stdin = saved_in
        assert val == "plain piped text"

    def test_piped_eof_returns_none(self):
        from graybox.cli import _interactive_input

        r, w = os.pipe()
        os.close(w)  # immediate EOF, no data
        saved_in = sys.stdin
        sys.stdin = os.fdopen(r, "r")
        try:
            val = _interactive_input("prompt> ")
        finally:
            sys.stdin = saved_in
        assert val is None


class TestSurrogatePairHandling:
    """Windows' getwch() delivers astral-plane characters (many emoji) as
    two unpaired surrogate halves across two reads; they must be recombined
    before being treated as one character anywhere else in the pipeline."""

    def test_recombines_a_real_astral_character(self):
        plant = "🪴"  # U+1FAB4, outside the Basic Multilingual Plane
        codepoint = ord(plant) - 0x10000
        high = chr(0xD800 + (codepoint >> 10))
        low = chr(0xDC00 + (codepoint & 0x3FF))
        assert _is_high_surrogate(high)
        assert _is_low_surrogate(low)
        assert _combine_surrogate_pair(high, low) == plant

    def test_bmp_characters_are_not_flagged_as_surrogates(self):
        for ch in ("a", "é", "中", "\n"):
            assert not _is_high_surrogate(ch)
            assert not _is_low_surrogate(ch)


class TestKeystrokeExceptionIsolation:
    """One keystroke's handling raising an unexpected error must not crash
    the whole capture session or lose text already typed."""

    @pytest.mark.skipif(sys.platform == "win32", reason="requires termios/pty")
    def test_mid_session_exception_is_isolated(self, monkeypatch):
        import graybox.cli as cli

        original_text_width = cli._text_width
        call_count = {"n": 0}

        def flaky_text_width(text):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated terminal glitch")
            return original_text_width(text)

        monkeypatch.setattr(cli, "_text_width", flaky_text_width)

        def setup(master):
            time.sleep(0.1)
            os.write(master, b"a")
            time.sleep(0.01)
            os.write(master, b"b")
            time.sleep(0.01)
            os.write(master, b"c")
            time.sleep(0.05)
            os.write(master, b"\r")

        val, err, alive = _drive_interactive_input(setup)
        assert not alive
        assert err is None, err
        assert val == "abc"
