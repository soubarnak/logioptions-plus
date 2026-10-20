"""One HID++ reader per node: events to listeners, replies to waiting requests (ticket #12)."""

import queue
import threading
import time

import pytest

from opl_agent import hidpp
from tests.fakes.hidpp_device import F_WAKE, MOUSE_INDEX


def open_reader(bolt):
    reader = hidpp.Reader(bolt.open(bolt.PATH))
    return reader


def test_event_reports_reach_listeners_and_not_the_request(bolt):
    reader = open_reader(bolt)
    seen = []
    got = threading.Event()

    def listener(index, feature_index, function, params):
        seen.append((index, feature_index, function))
        got.set()

    reader.add_listener(listener)
    try:
        bolt.emit_event(F_WAKE, function=0)
        assert got.wait(2)
        wake_index = hidpp.feature_index(reader, MOUSE_INDEX, F_WAKE)
        assert seen == [(MOUSE_INDEX, wake_index, 0)]
        assert hidpp.get_dpi(reader, MOUSE_INDEX)["dpi"] == 1000
    finally:
        reader.close()


def test_event_arriving_before_a_reply_does_not_disturb_the_request(bolt):
    reader = open_reader(bolt)
    try:
        for _ in range(20):
            bolt.emit_event(F_WAKE)
            assert hidpp.get_dpi(reader, MOUSE_INDEX)["dpi"] == 1000
    finally:
        reader.close()


def test_concurrent_requests_each_get_their_own_reply(bolt):
    reader = open_reader(bolt)
    failures = []

    def worker(read, expect):
        try:
            for _ in range(40):
                got = read()
                if got != expect:
                    failures.append((got, expect))
        except Exception as exc:        # a mismatched reply would also surface here
            failures.append(exc)

    jobs = [
        (lambda: hidpp.get_dpi(reader, MOUSE_INDEX)["dpi"], 1000),
        (lambda: hidpp.get_battery(reader, MOUSE_INDEX)["percent"], 73),
        (lambda: hidpp.get_smartshift(reader, MOUSE_INDEX)["threshold"], 30),
        (lambda: hidpp.get_dpi_list(reader, MOUSE_INDEX)["max"], 8000),
    ]
    threads = [threading.Thread(target=worker, args=j) for j in jobs * 2]
    try:
        for t in threads:
            t.start()
        for i in range(30):
            bolt.emit_event(F_WAKE)
        for t in threads:
            t.join(20)
        assert not failures, failures[:3]
        assert not any(t.is_alive() for t in threads)
    finally:
        reader.close()


def test_request_to_an_unknown_device_index_is_an_error(bolt):
    reader = open_reader(bolt)
    try:
        try:
            hidpp.root_ping(reader, 3, timeout=0.2)        # no device at index 3 answers
        except hidpp.HidppError as exc:
            assert not isinstance(exc, hidpp.HidppTimeout) and exc.name == "UNKNOWN_DEVICE"
        else:
            raise AssertionError("expected an error for an unknown device index")
    finally:
        reader.close()


class ScriptedHandle:
    """A transport the test drives by hand: it records writes and replays injected reports."""

    def __init__(self):
        self.writes = queue.Queue()
        self._in = queue.Queue()
        self.fail_read = None

    def write(self, data):
        self.writes.put(bytes(data))

    def inject(self, report):
        self._in.put(bytes(report).ljust(hidpp.LONG_LEN, b"\0"))

    def read(self, timeout):
        if self.fail_read is not None:
            raise self.fail_read
        try:
            return self._in.get(timeout=min(timeout, 0.05))
        except queue.Empty:
            return None

    def close(self):
        pass


def reply_to(request, params):
    return bytes([hidpp.LONG, request[1], request[2], request[3]]) + bytes(params)


def test_silence_raises_a_timeout_not_another_error():
    handle = ScriptedHandle()
    reader = hidpp.Reader(handle)
    try:
        with pytest.raises(hidpp.HidppTimeout):
            hidpp.call(reader, 1, 4, 2, timeout=0.1)
    finally:
        reader.close()


def test_a_late_reply_does_not_answer_the_next_identical_request():
    handle = ScriptedHandle()
    reader = hidpp.Reader(handle)
    try:
        with pytest.raises(hidpp.HidppTimeout):
            hidpp.call(reader, 1, 4, 2, b"\x01", timeout=0.1)
        first = handle.writes.get(timeout=1)
        got = []
        t = threading.Thread(target=lambda: got.append(hidpp.call(reader, 1, 4, 2, b"\x01", timeout=2)))
        t.start()
        second = handle.writes.get(timeout=1)
        assert first[3] != second[3]                      # a new software id for each request
        handle.inject(reply_to(first, b"\xAA"))           # the first request's answer, far too late
        handle.inject(reply_to(second, b"\xBB"))
        t.join(3)
        assert got and got[0][:1] == b"\xBB"
    finally:
        reader.close()


def test_a_read_failure_fails_waiting_and_later_requests_at_once():
    handle = ScriptedHandle()
    reader = hidpp.Reader(handle)
    errors = []

    def waiter():
        try:
            hidpp.call(reader, 1, 4, 2, timeout=5)
        except Exception as exc:
            errors.append(exc)

    try:
        t = threading.Thread(target=waiter)
        t.start()
        handle.writes.get(timeout=1)
        handle.fail_read = OSError(5, "gone")
        t.join(2)
        assert errors and isinstance(errors[0], OSError)
        assert reader.dead
        start = time.monotonic()
        with pytest.raises(OSError):
            hidpp.call(reader, 1, 4, 2, timeout=5)
        assert time.monotonic() - start < 1
    finally:
        reader.close()


def test_closing_the_reader_fails_a_waiting_request():
    handle = ScriptedHandle()
    reader = hidpp.Reader(handle)
    errors = []

    def waiter():
        try:
            hidpp.call(reader, 1, 4, 2, timeout=5)
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=waiter)
    t.start()
    handle.writes.get(timeout=1)
    reader.close()
    t.join(2)
    assert not t.is_alive() and errors


def test_hidpp10_notifications_reach_listeners_with_their_sub_id_and_address(bolt):
    reader = open_reader(bolt)
    seen = queue.Queue()
    reader.add_listener(lambda *ev: seen.put(ev))
    try:
        bolt.emit(bytes([hidpp.LONG, 0xFF, 0x54, 0x02, 0x00, 0x07]).ljust(hidpp.LONG_LEN, b"\0"))
        index, sub_id, address, data = seen.get(timeout=2)
        assert (index, sub_id, address) == (0xFF, 0x54, 0x02) and data[:2] == b"\x00\x07"
        assert hidpp.is_hidpp10_notification(sub_id)
    finally:
        reader.close()


def test_open_node_keeps_the_long_only_flag(ble, monkeypatch):
    monkeypatch.setattr(hidpp.HIDRAW, "open", ble.open)
    reader = hidpp.open_node(ble.PATH, long_only=True)
    try:
        assert reader.long_only is True
    finally:
        reader.close()
