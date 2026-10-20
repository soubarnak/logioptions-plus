"""The tracker that follows the focused window: a source that is not there yet, that keeps failing,
that cannot work at all, and what the agent does with it at stop."""

import asyncio
import logging

from opl_agent import focus, main, wayland_focus
from opl_agent.focus import Window
from tests.fakes.wayland import FakeCompositor
from tests.harness import wait_for


class Router:
    def __init__(self):
        self.seen = []

    def on_focus(self, window):
        self.seen.append(window)


class WantingRouter(Router):
    """A router that says whether any application has a profile."""

    def __init__(self, wants):
        super().__init__()
        self.wants = wants

    def has_app_profiles(self):
        return self.wants


class Script:
    """A window source that raises or answers as scripted, then repeats its last step."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def active(self):
        self.calls += 1
        step = self.steps[0] if len(self.steps) == 1 else self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def track(source, seconds=0.5, limit=4, **kw):
    router = Router()

    async def run():
        task = asyncio.ensure_future(main._track_focus(router, source, 0.001, fail_limit=limit, **kw))
        await asyncio.sleep(seconds)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    return router


def test_no_window_is_looked_at_while_no_application_has_a_profile():
    source = Script(Window(("kitty",)))
    router = WantingRouter(False)

    async def run():
        task = asyncio.ensure_future(main._track_focus(router, source, 0.001))
        await asyncio.sleep(0.2)
        assert source.calls == 0
        router.wants = True
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert router.seen == [None, Window(("kitty",))]          # the default first, then the focused window


def test_the_last_profile_going_away_puts_the_default_back():
    source = Script(Window(("kitty",)))
    router = WantingRouter(True)

    async def run():
        task = asyncio.ensure_future(main._track_focus(router, source, 0.001))
        await asyncio.sleep(0.2)
        router.wants = False
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert router.seen == [Window(("kitty",)), None]


def test_a_source_that_fails_forever_warns_once_and_falls_back_to_the_default_profile(caplog):
    source = Script(OSError("no compositor"))
    with caplog.at_level(logging.DEBUG, logger="opl.main"):
        router = track(source)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "no compositor" in warnings[0].getMessage()
    assert router.seen == [None]                              # told once: nothing is focused as far as we know
    assert source.calls > 4                                   # and it keeps trying


def test_a_source_that_comes_up_late_is_followed_from_then_on():
    window = Window(("kitty",))
    source = Script(OSError("not yet"), OSError("not yet"), window)
    router = track(source, limit=10)
    assert router.seen == [window]


def test_a_source_that_recovers_after_giving_up_reports_its_window():
    window = Window(("kitty",))
    source = Script(*([OSError("gone")] * 6 + [window]))
    router = track(source, limit=3)
    assert router.seen == [None, window]


def test_a_source_that_cannot_ever_work_stops_the_tracker_and_logs_why(caplog):
    class Never(wayland_focus.WaylandError):
        permanent = True

    source = Script(Never("no foreign-toplevel protocol"))
    with caplog.at_level(logging.INFO, logger="opl.main"):
        router = track(source)
    assert source.calls == 1 and router.seen == [None]
    assert any("default profile" in r.getMessage() for r in caplog.records)


# ---- the Wayland source is chosen without touching the compositor ----------------------------

def test_choosing_the_wayland_source_does_not_connect():
    calls = []

    def factory(env):
        calls.append(env)
        raise FileNotFoundError("the compositor is not up yet")
    src = focus.default_source(environ={"WAYLAND_DISPLAY": "wayland-1"}, wayland=factory)
    assert isinstance(src, wayland_focus.WaylandSource) and calls == []


def test_a_compositor_without_the_protocol_is_a_permanent_failure():
    comp = FakeCompositor(protocol=False)
    src = wayland_focus.WaylandSource(lambda: comp)
    try:
        src.active()
    except wayland_focus.WaylandError as exc:
        assert getattr(exc, "permanent", False)
    else:
        raise AssertionError("expected WaylandError")


def test_a_source_that_connects_later_works():
    comp = FakeCompositor()
    comp.open_window("kitty", active=True)
    attempts = []

    def connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise FileNotFoundError("not yet")
        return comp
    src = wayland_focus.WaylandSource(connect)
    for _ in range(2):
        try:
            src.active()
        except OSError:
            pass
    assert src.active().wm_class[0] == "kitty"


def test_the_window_side_carries_the_full_app_id_only():
    comp = FakeCompositor()
    comp.open_window("org.gnome.Terminal", active=True)
    window = wayland_focus.WaylandSource(lambda: comp).active()
    assert window.wm_class == ("org.gnome.Terminal",)
    assert focus.match_profile(window, ["xfce4-terminal"], [
        {"applicationId": "xfce4-terminal", "name": "Terminal"}]) is None


def test_stopping_the_agent_closes_the_window_source(agent_factory):
    class Closable:
        closed = False

        def active(self):
            return None

        def close(self):
            self.closed = True

    source = Closable()
    running = agent_factory(window_source=source, focus_seconds=0.02)
    running.stop()
    assert wait_for(lambda: source.closed)
