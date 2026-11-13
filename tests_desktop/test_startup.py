from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_app.startup import APPROVED_KEY, RUN_KEY, VALUE_NAME, SingleInstance, get_startup, set_startup
from desktop_app.main import DesktopBridge


class FakeRegistry:
    HKEY_CURRENT_USER = "user"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, key, *args):
        if key not in self.values:
            raise FileNotFoundError(key)
        return nullcontext(key)

    def CreateKeyEx(self, root, key, *args):
        self.values.setdefault(key, {})
        return nullcontext(key)

    def QueryValueEx(self, key, name):
        if name not in self.values[key]:
            raise FileNotFoundError(name)
        return self.values[key][name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[key][name] = value

    def DeleteValue(self, key, name):
        if name not in self.values[key]:
            raise FileNotFoundError(name)
        del self.values[key][name]


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe = Path(self.tmp.name) / "App with spaces" / "KaggleControlPlane.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.registry = FakeRegistry()
        self.enterContext(patch.dict(sys.modules, winreg=self.registry))
        self.enterContext(patch("desktop_app.startup.sys.platform", "win32"))

    def test_enable_quotes_executable_and_disable_preserves_other_apps(self):
        self.registry.values[RUN_KEY] = {"OtherApp": "keep"}
        self.assertFalse(get_startup(self.exe)["enabled"])
        self.assertTrue(set_startup(True, self.exe)["enabled"])
        self.assertEqual(self.registry.values[RUN_KEY][VALUE_NAME], f'"{self.exe.resolve()}"')
        self.assertTrue(set_startup(True, self.exe)["enabled"])
        self.assertFalse(set_startup(False, self.exe)["enabled"])
        self.assertFalse(set_startup(False, self.exe)["enabled"])
        self.assertEqual(self.registry.values[RUN_KEY], {"OtherApp": "keep"})

    def test_task_manager_disable_is_visible_and_explicit_toggle_repairs_only_own_flag(self):
        set_startup(True, self.exe)
        self.registry.values[APPROVED_KEY] = {VALUE_NAME: b"\x03" + b"\x00" * 11, "OtherApp": b"\x03"}
        state = get_startup(self.exe)
        self.assertTrue(state["registered"])
        self.assertTrue(state["disabled_by_windows"])
        self.assertFalse(state["enabled"])
        self.assertTrue(set_startup(True, self.exe)["enabled"])
        self.assertEqual(self.registry.values[APPROVED_KEY], {"OtherApp": b"\x03"})

    def test_invalid_input_or_missing_executable_does_not_register(self):
        for value in ("false", 1, None):
            with self.assertRaises(ValueError):
                set_startup(value, self.exe)
        with self.assertRaises(ValueError):
            set_startup(True, self.exe.with_name("missing.exe"))
        self.assertEqual(self.registry.values, {})

    def test_development_source_is_not_registered_as_python_startup(self):
        with patch("desktop_app.startup.sys.frozen", False, create=True):
            self.assertFalse(get_startup()["supported"])
            with self.assertRaises(ValueError):
                set_startup(True)
        self.assertEqual(self.registry.values, {})

    def test_stale_target_is_not_reported_enabled(self):
        self.registry.values[RUN_KEY] = {VALUE_NAME: '"C:\\old\\KaggleControlPlane.exe"'}
        self.assertFalse(get_startup(self.exe)["enabled"])

    def test_registry_error_is_reported_to_desktop_ui(self):
        bridge = DesktopBridge(SimpleNamespace())
        with patch("desktop_app.main.set_startup", side_effect=PermissionError("Windows denied this setting")):
            self.assertEqual(bridge.set_startup(True), {"ok": False, "error": "Windows denied this setting"})


@unittest.skipUnless(sys.platform == "win32", "Windows mutex")
class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_rejected_and_handle_is_released(self):
        with tempfile.TemporaryDirectory() as scope, patch.dict("os.environ", LOCALAPPDATA=scope):
            first, second, third = SingleInstance(), SingleInstance(), SingleInstance()
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.close()
                self.assertTrue(third.acquire())
            finally:
                first.close()
                second.close()
                third.close()


if __name__ == "__main__":
    unittest.main()
