import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desktop_app.main import DesktopBridge


class DatasetPickerTests(unittest.TestCase):
    def test_native_selection_returns_preview_without_upload(self):
        datasets = Mock()
        datasets.select.return_value = {"selection_id": "opaque", "file_count": 1, "bytes": 9, "files": [{"name": "data.pth", "bytes": 9}]}
        window = Mock()
        window.create_file_dialog.return_value = ("C:/chosen/data.pth",)
        runtime = SimpleNamespace(window=window, service=SimpleNamespace(datasets=datasets))
        webview = SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, FOLDER=20))
        with patch.dict(sys.modules, {"webview": webview}):
            result = DesktopBridge(runtime).choose_dataset_files(False)
        self.assertTrue(result["ok"])
        self.assertNotIn("path", result)
        window.create_file_dialog.assert_called_once_with(10, allow_multiple=True)
        datasets.select.assert_called_once_with(("C:/chosen/data.pth",), folder=False)
        datasets.submit.assert_not_called()

    def test_cancelled_dialog_does_not_register_or_upload(self):
        datasets = Mock()
        window = Mock()
        window.create_file_dialog.return_value = None
        runtime = SimpleNamespace(window=window, service=SimpleNamespace(datasets=datasets))
        webview = SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, FOLDER=20))
        with patch.dict(sys.modules, {"webview": webview}):
            self.assertTrue(DesktopBridge(runtime).choose_dataset_files(True)["cancelled"])
        window.create_file_dialog.assert_called_once_with(20, allow_multiple=False)
        datasets.select.assert_not_called()
