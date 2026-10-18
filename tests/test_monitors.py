"""Tests for the monitor (DDC/CI) half of multi_input_switcher.pyw.

Nothing here talks to a real monitor, so these run on any platform:
    python -m unittest discover -s tests -v
"""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_PATH = Path(__file__).resolve().parent.parent / "multi_input_switcher.pyw"
_LOADER = importlib.machinery.SourceFileLoader("multi_input_switcher", str(_PATH))
tis = importlib.util.module_from_spec(importlib.util.spec_from_loader("multi_input_switcher", _LOADER))
sys.modules["multi_input_switcher"] = tis
_LOADER.exec_module(tis)

DELL = (
    "(prot(monitor)type(LCD)model(U2720Q)cmds(01 02 03 07 0C E3 F3)vcp(02 04 05 08 10 12 "
    "14(01 04 05 06 08 09 0B 0C) 16 18 1A 52 60(0F 11 1B) AA(01 02 04) AC AE B2 B6 C6 C8 C9 "
    "D6(01 04 05) DC(00 03 05) DF E0 E1 E2(00 1D 01 02 04 0E 12 14 23 24 27) F0(00 0C) F1 F2 FD)"
    "mswhql(1)asset_eep(40)mccs_ver(2.1))"
)

# Sloppy but real-world: spaces inside parens, no outer parens, trailing NUL.
LG = (
    "prot(monitor)type(lcd)model(LG ULTRAGEAR)cmds(01 02 03 0C E3 F3)"
    "vcp(02 04 05 08 10 12 14(05 08 0B ) 16 18 1A 52 60( 11 12 0F) AC AE B2 B6 C0 C6 C8 C9 "
    "D6(01 04) DF 62 8D F4 F5(00 01 02) F6(00 01 02) 4D 4E 4F 15(01 06 09 10 11 13 14 28 29 32 44 48) "
    "F7(00 01 02 03) F8(00 01) F9 E4 E5 E6 E7 E8 E9 EA EB EF FD(00 01) FE(00 01 02) FF)"
    "mccs_ver(2.1)mswhql(1)\x00"
)


class ParseCapabilitiesTest(unittest.TestCase):
    def test_dell(self):
        caps = tis.parse_capabilities(DELL)
        self.assertEqual(caps["model"], "U2720Q")
        self.assertEqual(caps["mccs_ver"], "2.1")
        self.assertEqual(caps["vcp"][0x60], [0x0F, 0x11, 0x1B])
        self.assertEqual(caps["vcp"][0x10], [])
        self.assertEqual(caps["vcp"][0xE2][:3], [0x00, 0x1D, 0x01])
        self.assertIn(0xFD, caps["vcp"])

    def test_lg_without_outer_parens(self):
        caps = tis.parse_capabilities(LG)
        self.assertEqual(caps["model"], "LG ULTRAGEAR")
        self.assertEqual(caps["vcp"][0x60], [0x11, 0x12, 0x0F])
        self.assertEqual(caps["vcp"][0x15][-1], 0x48)
        self.assertEqual(tis.supported_inputs(LG), [0x11, 0x12, 0x0F])

    def test_missing_input_list(self):
        self.assertEqual(tis.supported_inputs("(vcp(10 12 60 62))"), [])

    def test_empty_and_garbage(self):
        self.assertEqual(tis.parse_capabilities(""), {})
        self.assertEqual(tis.parse_capabilities(None), {})
        self.assertEqual(tis.supported_inputs("not a caps string"), [])
        self.assertEqual(tis.supported_inputs("(vcp(60(0F 11"), [0x0F, 0x11])

    def test_codes_without_spaces(self):
        self.assertEqual(
            tis.parse_capabilities("(vcp(021060(0F11)))")["vcp"], {0x02: [], 0x10: [], 0x60: [0x0F, 0x11]})


class PowerModeTest(unittest.TestCase):
    def test_off_value_from_capabilities(self):
        dell = tis.parse_capabilities(DELL)["vcp"][tis.VCP_POWER_MODE]
        lg = tis.parse_capabilities(LG)["vcp"][tis.VCP_POWER_MODE]
        self.assertEqual(dell, [0x01, 0x04, 0x05])
        self.assertEqual(tis.best_power_off_value(dell), 0x04)
        self.assertEqual(tis.best_power_off_value(lg), 0x04)

    def test_off_value_prefers_modes_that_can_wake(self):
        self.assertEqual(tis.best_power_off_value([0x01, 0x02, 0x05]), 0x02)
        self.assertEqual(tis.best_power_off_value([0x01, 0x05]), 0x05)
        # Listed without values, or not at all: DPM off.
        self.assertEqual(tis.best_power_off_value([]), 0x04)


class InputNamesTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(tis.input_name(0x11), "HDMI 1")
        self.assertEqual(tis.input_name(0x0F), "DisplayPort 1")
        self.assertEqual(tis.input_name(0x90), "Input 0x90")

    def test_parse_input(self):
        self.assertEqual(tis.parse_input("hdmi1"), 0x11)
        self.assertEqual(tis.parse_input("HDMI 2"), 0x12)
        self.assertEqual(tis.parse_input("dp1"), 0x0F)
        self.assertEqual(tis.parse_input("DisplayPort-2"), 0x10)
        self.assertEqual(tis.parse_input("usb-c"), 0x1B)
        self.assertEqual(tis.parse_input("0x11"), 0x11)
        self.assertEqual(tis.parse_input("17"), 0x11)
        for bad in ("hdmi9", "0x100", "-1", ""):
            with self.assertRaises(ValueError):
                tis.parse_input(bad)


class MissingModuleTest(unittest.TestCase):
    def test_reads_module_from_message(self):
        self.assertEqual(tis._missing_module(ImportError("No module named 'pystray'")), "pystray")
        self.assertEqual(tis._missing_module(ImportError("No module named 'PIL._imaging'")), "PIL")
        # pystray wraps a missing dependency in its own error.
        wrapped = ImportError("this platform is not supported: No module named 'six'")
        self.assertEqual(tis._missing_module(wrapped), "six")
        self.assertIsNone(tis._missing_module(ImportError("DLL load failed while importing _imaging")))


def fake_monitor(name="DELL U2720Q", inputs=(0x11, 0x0F, 0x1B), supported=True):
    return SimpleNamespace(name=name, inputs=list(inputs), supported=supported)


class MonitorConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = tis.MonitorConfig()
        self.assertEqual(config.poll_seconds, 30)
        self.assertFalse(config.hide_unsupported)
        self.assertEqual(config.label_for(fake_monitor()), "DELL U2720Q")
        self.assertEqual(
            config.inputs_for(fake_monitor()),
            [(0x0F, "DisplayPort 1"), (0x11, "HDMI 1"), (0x1B, "USB-C")],
        )

    def test_overrides(self):
        config = tis.MonitorConfig({"monitors": {"dell u2720q": {
            "label": "Left",
            "inputs": {"0x0F": "Desktop", "0x12": "Console", "17": "Laptop", "bogus": "x"},
            "hide": ["0x1B"],
        }}})
        m = fake_monitor()
        self.assertEqual(config.label_for(m), "Left")
        self.assertEqual(
            config.inputs_for(m),
            [(0x0F, "Desktop"), (0x11, "Laptop"), (0x12, "Console")],
        )
        # Other monitors are unaffected.
        self.assertEqual(config.label_for(fake_monitor("LG")), "LG")

    def test_power_off_override(self):
        config = tis.MonitorConfig({"monitors": {
            "DELL U2720Q": {"power_off": "0x05"},
            "LG": {"power_off": "nope"},
        }})
        self.assertEqual(config.power_off_value(fake_monitor()), 0x05)
        self.assertIsNone(config.power_off_value(fake_monitor("LG")))
        self.assertIsNone(config.power_off_value(fake_monitor("Other")))

    def test_parse_code(self):
        self.assertEqual(tis.parse_code("0x0F"), 15)
        self.assertEqual(tis.parse_code("15"), 15)
        self.assertEqual(tis.parse_code(17), 17)
        self.assertIsNone(tis.parse_code("0F"))
        self.assertIsNone(tis.parse_code("0x1FF"))

    def test_load_and_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.json"
            config, error = tis.MonitorConfig.load(path)
            self.assertIsNone(error)
            self.assertEqual(config.poll_seconds, 30)

            tis.write_template([fake_monitor(), fake_monitor("Laptop", supported=False)], path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(data["monitors"]), ["DELL U2720Q"])
            self.assertEqual(data["monitors"]["DELL U2720Q"]["inputs"]["0x0F"], "DisplayPort 1")
            config, error = tis.MonitorConfig.load(path)
            self.assertIsNone(error)
            self.assertEqual(config.inputs_for(fake_monitor())[0], (0x0F, "DisplayPort 1"))

            path.write_text("{not json", encoding="utf-8")
            config, error = tis.MonitorConfig.load(path)
            self.assertIn("invalid", error)
            self.assertEqual(config.poll_seconds, 30)


class MonitorManagerTest(unittest.TestCase):
    """The manager's logic, with the DDC/CI calls replaced by mocks."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in (("MONITOR_CONFIG_PATH", root / "monitors.json"),
                            ("LEGACY_MONITOR_CONFIG_PATH", root / "old" / "config.json")):
            patcher = mock.patch.object(tis, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # These only exist on Windows, hence create=True.
        self.set_vcp = self.patch("set_vcp")
        self.patch("_DestroyPhysicalMonitor")

        self.changes, self.notes = [], []
        self.manager = tis.MonitorManager(lambda: self.changes.append(1), self.notes.append)
        self.addCleanup(self.manager.stop)
        self.manager.busy = False
        self.manager.generation = 3

        def monitor(index, name, current):
            return tis.Monitor(
                name=name, device=f"\\\\.\\DISPLAY{index}", position=(index * 1000, 0), primary=index == 1,
                handle=index, index=index, inputs=[0x0F, 0x11], current_input=current,
                power_off_value=0x04, power_on=True,
            )

        self.manager.monitors = [monitor(1, "DELL", 0x0F), monitor(2, "LG", 0x11)]

    def patch(self, name):
        patcher = mock.patch.object(tis, name, create=True)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_snapshot(self):
        snap = self.manager.snapshot()
        self.assertTrue(snap["monitors_supported"])
        self.assertEqual(snap["monitors_gen"], 3)
        self.assertEqual([m["title"] for m in snap["monitors"]], ["DELL", "LG"])
        dell = snap["monitors"][0]
        self.assertEqual(dell["status"], "DisplayPort 1")
        self.assertEqual(dell["current"], 0x0F)
        self.assertEqual(dell["inputs"], [(0x0F, "DisplayPort 1"), (0x11, "HDMI 1")])
        self.assertTrue(dell["can_power"])

    def test_snapshot_hides_unsupported_when_asked(self):
        broken = tis.Monitor(name="Panel", device="d", position=(0, 0), primary=False, handle=None,
                             index=3, error="no DDC/CI")
        self.manager.monitors.append(broken)
        self.assertEqual(len(self.manager.snapshot()["monitors"]), 3)
        self.manager.config.data["hide_unsupported"] = True
        self.assertEqual(len(self.manager.snapshot()["monitors"]), 2)

    def test_power_off_shows_in_status_and_tooltip(self):
        self.manager.monitors[1].power_on = False
        self.assertEqual(self.manager.snapshot()["monitors"][1]["status"], "Off")
        self.assertEqual(self.manager.tooltip_lines(), ["DELL: DisplayPort 1", "LG: Off"])

    def test_duplicate_names_are_numbered(self):
        self.manager.monitors[1].name = "DELL"
        self.assertEqual([m["title"] for m in self.manager.snapshot()["monitors"]], ["DELL (1)", "DELL (2)"])

    def test_switch_sends_input_select(self):
        self.manager._switch(3, 2, 0x0F)
        self.set_vcp.assert_called_once_with(2, tis.VCP_INPUT_SOURCE, 0x0F)
        self.assertEqual(self.manager.monitors[1].current_input, 0x0F)
        self.assertTrue(self.changes)

    def test_stale_or_busy_clicks_are_ignored(self):
        self.manager._switch(2, 2, 0x0F)  # menu built before the last refresh
        self.manager.busy = True
        self.manager._switch(3, 2, 0x0F)
        self.manager._power(3, None, False)
        self.set_vcp.assert_not_called()

    def test_switch_failure_notifies(self):
        self.set_vcp.side_effect = OSError("bus error")
        self.manager._switch(3, 1, 0x11)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("Couldn't switch DELL to HDMI 1", self.notes[0])
        self.assertEqual(self.manager.monitors[0].current_input, 0x0F)

    def test_power_all_and_one(self):
        self.manager._power(3, None, False)
        self.assertEqual(self.set_vcp.call_args_list, [
            mock.call(1, tis.VCP_POWER_MODE, 0x04), mock.call(2, tis.VCP_POWER_MODE, 0x04)])
        self.set_vcp.reset_mock()
        self.manager._power(3, [2], True)
        self.set_vcp.assert_called_once_with(2, tis.VCP_POWER_MODE, tis.POWER_ON)
        self.assertTrue(self.manager.monitors[1].power_on)

    def test_power_uses_configured_off_value(self):
        self.manager.config.data["monitors"] = {"DELL": {"power_off": "0x05"}}
        self.manager._power(3, [1], False)
        self.set_vcp.assert_called_once_with(1, tis.VCP_POWER_MODE, 0x05)

    def test_poll_picks_up_changes_made_on_the_monitor(self):
        with mock.patch.object(tis.Monitor, "read_input", side_effect=[0x11, 0x0F]), \
                mock.patch.object(tis.Monitor, "read_power", return_value=False):
            self.manager._poll()
        self.assertEqual([m.current_input for m in self.manager.monitors], [0x11, 0x0F])
        self.assertEqual([m.power_on for m in self.manager.monitors], [False, False])
        self.assertTrue(self.changes)

    def test_old_config_is_copied_once(self):
        legacy = tis.LEGACY_MONITOR_CONFIG_PATH
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"poll_seconds": 7}), encoding="utf-8")
        manager = tis.MonitorManager(lambda: None, lambda m: None)
        self.addCleanup(manager.stop)
        self.assertEqual(manager.config.poll_seconds, 7)
        self.assertTrue(tis.MONITOR_CONFIG_PATH.exists())
        legacy.write_text(json.dumps({"poll_seconds": 99}), encoding="utf-8")
        tis.MonitorManager(lambda: None, lambda m: None).stop()
        self.assertEqual(json.loads(tis.MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))["poll_seconds"], 7)


class MigrateOldNameTest(unittest.TestCase):
    def test_settings_move_to_the_new_names_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, appdata = Path(tmp) / "home", Path(tmp) / "appdata"
            (appdata / "TVInputSwitcher").mkdir(parents=True)
            home.mkdir()
            (home / ".tv_input_switcher.json").write_text('{"device_id": "tv"}', encoding="utf-8")
            (appdata / "TVInputSwitcher" / "monitors.json").write_text('{"poll_seconds": 9}', encoding="utf-8")
            new_config, new_monitors = home / ".multi_input_switcher.json", appdata / "Multi" / "monitors.json"
            with mock.patch.object(Path, "home", return_value=home), \
                    mock.patch.dict("os.environ", {"APPDATA": str(appdata)}), \
                    mock.patch.object(tis, "IS_WIN", False), \
                    mock.patch.object(tis, "CONFIG_PATH", new_config), \
                    mock.patch.object(tis, "MONITOR_CONFIG_PATH", new_monitors):
                tis.migrate_old_name()
                self.assertEqual(json.loads(new_config.read_text(encoding="utf-8")), {"device_id": "tv"})
                self.assertEqual(json.loads(new_monitors.read_text(encoding="utf-8")), {"poll_seconds": 9})
                self.assertFalse((home / ".tv_input_switcher.json").exists())
                # A later run must not overwrite settings changed under the new name.
                new_config.write_text('{"device_id": "changed"}', encoding="utf-8")
                (home / ".tv_input_switcher.json").write_text('{"device_id": "stale"}', encoding="utf-8")
                tis.migrate_old_name()
                self.assertEqual(json.loads(new_config.read_text(encoding="utf-8")), {"device_id": "changed"})


@unittest.skipUnless(tis.HAVE_TRAY, "pystray isn't installed")
class TrayMenuTest(unittest.TestCase):
    """Runs the real tray process code against a fake icon, then reads its menu."""

    def setUp(self):
        import queue
        import threading

        self.state_q, self.cmd_q = queue.Queue(), queue.Queue()
        self.icon = None
        self.finished = threading.Event()
        test = self

        class FakeIcon:
            def __init__(self, name, image, title, menu):
                self.menu, self.title, self.visible, self.notes = menu, title, False, []
                test.icon = self

            def run(self, setup=None):
                setup(self)
                test.finished.wait(5)

            def update_menu(self):
                pass

            def stop(self):
                test.finished.set()

            def notify(self, text, title=None):
                self.notes.append(text)

        patcher = mock.patch.object(tis, "TrayIcon", FakeIcon)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(target=tis._run_tray, args=(self.state_q, self.cmd_q), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.finished.set)

    def push(self, **snapshot):
        import time
        self.state_q.put(snapshot)
        time.sleep(0.3)  # let the listener thread apply it
        return {i.text: i for i in self.icon.menu}

    @staticmethod
    def monitor(index, title, current=0x0F, can_power=True, supported=True):
        return {"index": index, "title": title, "supported": supported, "status": "DisplayPort 1",
                "inputs": [(0x0F, "DisplayPort 1"), (0x11, "HDMI 1")], "current": current,
                "can_power": can_power}

    def test_no_monitor_items_when_unsupported(self):
        menu = self.push(monitors_supported=False)
        self.assertNotIn("Refresh monitors", menu)
        self.assertNotIn("Start with Windows", menu)
        self.assertIn("Open log", menu)

    def test_several_monitors_get_submenus_and_power_all(self):
        menu = self.push(monitors_supported=True, monitors_gen=4, monitors=[
            self.monitor(1, "DELL"), self.monitor(2, "LG", current=0x11)])
        dell = menu["DELL\tDisplayPort 1"]
        self.assertEqual([i.text for i in dell.submenu if i.text != "- - - -"],
                         ["DisplayPort 1", "HDMI 1", "Power on", "Power off"])
        hdmi = [i for i in dell.submenu if i.text == "HDMI 1"][0]
        hdmi(self.icon)
        self.assertEqual(self.cmd_q.get(timeout=1), ("monitor_input", 4, 1, 0x11))
        menu["Power off all monitors"](self.icon)
        self.assertEqual(self.cmd_q.get(timeout=1), ("monitor_power", 4, None, False))
        menu["Refresh monitors"](self.icon)
        self.assertEqual(self.cmd_q.get(timeout=1), ("monitor_refresh",))
        radio = {i.text: i.checked for i in dell.submenu if i.text in ("DisplayPort 1", "HDMI 1")}
        self.assertEqual(radio, {"DisplayPort 1": True, "HDMI 1": False})

    def test_single_monitor_is_listed_flat(self):
        menu = self.push(monitors_supported=True, monitors_gen=1, monitors=[self.monitor(1, "DELL")])
        self.assertIn("DELL\tDisplayPort 1", menu)
        self.assertFalse(menu["DELL\tDisplayPort 1"].enabled)
        self.assertIn("HDMI 1", menu)
        self.assertNotIn("Power off all monitors", menu)
        menu["Power off"](self.icon)
        self.assertEqual(self.cmd_q.get(timeout=1), ("monitor_power", 1, [1], False))

    def test_unsupported_busy_and_empty(self):
        menu = self.push(monitors_supported=True, monitors_busy=True, monitors=[])
        self.assertFalse(menu["Detecting monitors…"].enabled)
        self.assertFalse(menu["Refresh monitors"].enabled)
        menu = self.push(monitors_supported=True, monitors_busy=False, monitors=[])
        self.assertIn("No monitors found", menu)
        menu = self.push(monitors_supported=True, monitors=[self.monitor(1, "Panel", supported=False)])
        self.assertIn("Panel  (no DDC/CI)", menu)

    def test_tooltip_and_notification(self):
        self.push(tooltip="Multi-Input-Switcher\nDELL: HDMI 1", notify="Couldn't switch DELL")
        self.assertEqual(self.icon.title, "Multi-Input-Switcher\nDELL: HDMI 1")
        self.assertEqual(self.icon.notes, ["Couldn't switch DELL"])


if __name__ == "__main__":
    unittest.main()
