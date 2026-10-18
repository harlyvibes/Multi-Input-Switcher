"""The monitor controls in the main window (Windows only, needs a display)."""
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

from test_monitors import tis


def buttons(widget):
    """Every ttk.Button under *widget*, keyed by text (the first one wins)."""
    found = {}
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            found.setdefault(child.cget("text"), child)
        found.update({k: v for k, v in buttons(child).items() if k not in found})
    return found


def labels(widget):
    out = []
    for child in widget.winfo_children():
        if isinstance(child, ttk.Label):
            out.append(child.cget("text"))
        out += labels(child)
    return out


@unittest.skipUnless(tis.IS_WIN, "monitor controls are Windows only")
class MonitorPanelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in (("MONITOR_CONFIG_PATH", root / "monitors.json"),
                            ("LEGACY_MONITOR_CONFIG_PATH", root / "none.json"),
                            ("CONFIG_PATH", root / "config.json"),
                            ("HAVE_TRAY", False)):
            self.patch(name, value)
        self.patch("_DestroyPhysicalMonitor", mock.Mock())

        def monitor(index, name, current):
            return tis.Monitor(name=name, device=f"d{index}", position=(index * 100, 0), primary=index == 1,
                               handle=index, index=index)

        self.current = {"DELL": 0x0F, "LG": 0x11}

        def probe(this):
            this.inputs, this.current_input = [0x0F, 0x11], self.current[this.name]
            this.power_off_value, this.power_on = 0x04, True

        self.monitors = [monitor(1, "DELL", 0), monitor(2, "LG", 0)]
        self.patch("enumerate_monitors", lambda: self.monitors)
        patcher = mock.patch.object(tis.Monitor, "probe", probe)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("load_tvs", "open_account"):  # no SmartThings calls or dialogs
            patcher = mock.patch.object(tis.App, name, lambda self: None)
            patcher.start()
            self.addCleanup(patcher.stop)

        try:
            self.app = tis.App()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.addCleanup(self.destroy)
        self.pump(lambda: self.app.mon.generation > 0 and "Power off all" in buttons(self.app.monitor_panel))

    def patch(self, name, value):
        patcher = mock.patch.object(tis, name, value, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def destroy(self):
        self.app.mon.stop()
        self.app.mon = None
        self.app.update()  # let queued callbacks run before the window goes
        self.app.destroy()

    def pump(self, done, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            self.app.update()
            if done():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for the window to update")

    def test_panel_lists_each_monitor_with_its_inputs_and_power(self):
        panel = self.app.monitor_panel
        self.assertEqual(sorted(labels(panel)), ["DELL — DisplayPort 1", "LG — HDMI 1"])
        found = buttons(panel)
        self.assertIn("● DisplayPort 1", found)  # DELL's current input is marked
        self.assertIn("● HDMI 1", found)  # and LG's
        for text in ("Power on", "Power off", "Power on all", "Power off all"):
            self.assertIn(text, found)

    def test_buttons_send_the_right_commands(self):
        mon = self.app.mon
        mon.switch, mon.power = mock.Mock(), mock.Mock()
        gen = mon.generation
        buttons(self.app.monitor_panel)["● DisplayPort 1"].invoke()  # DELL's button comes first
        mon.switch.assert_called_with(gen, 1, 0x0F)
        buttons(self.app.monitor_panel)["Power off all"].invoke()
        mon.power.assert_called_with(gen, None, False)
        buttons(self.app.monitor_panel)["Power on"].invoke()
        mon.power.assert_called_with(gen, [1], True)

    def test_panel_follows_changes_made_elsewhere(self):
        self.app.mon.monitors[0].current_input = 0x11
        self.app.mon.monitors[1].power_on = False
        self.app.mon.on_change()
        self.pump(lambda: "DELL — HDMI 1" in labels(self.app.monitor_panel))
        self.assertIn("LG — Off", labels(self.app.monitor_panel))

    def test_panel_is_not_rebuilt_when_nothing_changed(self):
        first = self.app.monitor_panel.winfo_children()
        self.app.update_monitor_panel()
        self.assertEqual(self.app.monitor_panel.winfo_children(), first)

    def test_unsupported_monitor_is_listed_but_has_no_buttons(self):
        broken = tis.Monitor(name="Panel", device="d3", position=(300, 0), primary=False, handle=None,
                             index=3, error="no DDC/CI")
        self.app.mon.monitors.append(broken)
        self.app.update_monitor_panel()
        self.assertIn("Panel  (no DDC/CI)", labels(self.app.monitor_panel))


if __name__ == "__main__":
    unittest.main()
