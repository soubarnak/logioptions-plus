"""One HID++ reader per node: events to listeners, replies to waiting requests (ticket #12)."""

import threading

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


def test_request_times_out_when_the_device_stays_silent(bolt):
    reader = open_reader(bolt)
    try:
        try:
            hidpp.root_ping(reader, 3, timeout=0.2)        # no device at index 3 answers
        except hidpp.HidppError:
            pass
        else:
            raise AssertionError("expected an error for an unknown device index")
    finally:
        reader.close()
