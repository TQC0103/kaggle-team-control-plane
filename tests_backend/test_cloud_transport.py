import unittest
from unittest.mock import patch

from cloud_orchestrator.transport import read_output_json, TransportError


class Response:
    def __init__(self,status=200,location=None,body=b'{"PASS":true}'):
        self.status_code,self.headers,self.body=status,{'Location':location},body
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def iter_content(self,n):yield self.body


class TransportTests(unittest.TestCase):
    def test_official_gateway_to_storage(self):
        with patch('requests.get',side_effect=[Response(302,'https://storage.googleapis.com/receipt'),Response()]) as get:
            self.assertEqual(read_output_json('https://www.kaggleusercontent.com/receipt',100),{'PASS':True})
            self.assertEqual(get.call_count,2)
            for call in get.call_args_list:
                self.assertFalse(call.kwargs['allow_redirects'])
                self.assertNotIn('headers',call.kwargs)
                self.assertNotIn('auth',call.kwargs)
    def test_untrusted_redirect_never_requested(self):
        with patch('requests.get',return_value=Response(302,'https://attacker.test/leak')) as get:
            with self.assertRaises(TransportError):read_output_json('https://www.kaggleusercontent.com/receipt',100)
            self.assertEqual(get.call_count,1)
    def test_no_userinfo_http_nondefault_port_or_lookalike(self):
        with patch('requests.get') as get:
            for url in ['http://storage.googleapis.com/x','https://a@storage.googleapis.com/x',
                        'https://storage.googleapis.com:999/x','https://storage.googleapis.com.evil.test/x']:
                with self.assertRaises(TransportError):read_output_json(url,100)
            get.assert_not_called()
    def test_bounded_content(self):
        with patch('requests.get',return_value=Response(body=b'x'*101)):
            with self.assertRaises(TransportError):read_output_json('https://storage.googleapis.com/receipt',100)
    def test_not_ready_is_not_a_valid_receipt(self):
        with patch('requests.get',return_value=Response(status=403)):
            with self.assertRaises(TransportError):read_output_json('https://storage.googleapis.com/receipt',100)


if __name__=='__main__':unittest.main()
