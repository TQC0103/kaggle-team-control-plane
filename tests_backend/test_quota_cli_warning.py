import json
import threading
import unittest
from unittest.mock import patch
from control_plane.adapters import KaggleCliAdapter,AdapterError

class QuotaWarningTests(unittest.TestCase):
    rows=[dict(resource='GPU',used='22.82h',remaining='7.18h',total='30.00h',refreshAt='2026-10-24T00:00:00')]
    def quota(self,text):
        a=KaggleCliAdapter()
        with patch.object(a,'_run',return_value=text):return a.quota({},threading.Event())
    def test_plain_json(self):
        self.assertEqual(self.quota(json.dumps(self.rows))['resources']['gpu']['remaining_hours'],7.18)
    def test_official_version_notice(self):
        notice="Warning: Looks like you're using an outdated API Version, please consider updating (server 1.8.3 / client 1.7.4.5)\r\n"
        self.assertEqual(self.quota(notice+json.dumps(self.rows)),self.quota(json.dumps(self.rows)))
    def test_observed_cli_version_notice_with_blank_line(self):
        notice="Warning: Looks like you're using an outdated `kaggle` version (installed: 2.2.4), please consider upgrading to the latest version (2.2.5)\n\n"
        self.assertEqual(self.quota(notice+json.dumps(self.rows)),self.quota(json.dumps(self.rows)))
    def test_non_quota_prefix_and_suffix_fail_closed(self):
        for text in ['Unauthorized\n','<html>login</html>\n','Warning: credentials expired\n']:
            with self.subTest(text=text),self.assertRaises(AdapterError):self.quota(text+json.dumps(self.rows))
        with self.assertRaises(AdapterError):self.quota(json.dumps(self.rows)+'\nERROR')
    def test_wrong_shape_still_rejected(self):
        for value in [dict(error='expired'),[],[dict(resource='GPU',used='invalid')]]:
            with self.subTest(value=value),self.assertRaises(AdapterError):self.quota(json.dumps(value))

if __name__=='__main__':unittest.main()
