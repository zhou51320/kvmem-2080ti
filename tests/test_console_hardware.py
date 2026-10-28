import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
import urllib.request
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from console_hardware import SystemDiskSampler


class DiskTests(unittest.TestCase):
    def test_rates_use_actual_monotonic_interval_and_cache(self):
        reader = Mock(side_effect=[SimpleNamespace(read_bytes=100, write_bytes=200),
                                   SimpleNamespace(read_bytes=500, write_bytes=1000)])
        sampler = SystemDiskSampler(reader, Mock(side_effect=[10, 12]), Mock(side_effect=[100, 90]))
        sampler.sample()
        self.assertIsNone(sampler.snapshot()['system_disk_read_bytes_per_second'])
        sampler.sample()
        value = sampler.snapshot()
        self.assertEqual(value['system_disk_read_bytes_per_second'], 200)
        self.assertEqual(value['system_disk_write_bytes_per_second'], 400)
        self.assertEqual(value['interval_ms'], 2000)
        self.assertEqual(value['sampled_at_ms'], 90000)
        for _ in range(20):
            self.assertEqual(sampler.snapshot(), value)
        self.assertEqual(reader.call_count, 2)
        value['status'] = 'tampered'
        self.assertEqual(sampler.snapshot()['status'], 'ready')

    def test_failures_reset_and_recovery_never_publish_false_zero_or_negative(self):
        counts = lambda n: SimpleNamespace(read_bytes=n, write_bytes=2*n)
        reader = Mock(side_effect=[counts(100), None, counts(200), OSError(),
                                   counts(300), counts(10), counts(20), counts(20)])
        sampler = SystemDiskSampler(reader, Mock(side_effect=range(10)), lambda: 100)
        for _ in range(6):
            sampler.sample()
            self.assertIsNone(sampler.snapshot()['system_disk_read_bytes_per_second'])
        sampler.sample()
        self.assertEqual(sampler.snapshot()['system_disk_read_bytes_per_second'], 10)
        sampler.sample()
        self.assertEqual(sampler.snapshot()['system_disk_read_bytes_per_second'], 0)

    def test_sampler_lifecycle(self):
        read = threading.Event()
        def reader():
            read.set()
            return None
        sampler = SystemDiskSampler(reader).start()
        try:
            self.assertTrue(read.wait(2))
            self.assertIs(sampler.start(), sampler)
        finally:
            sampler.close()
        self.assertFalse(sampler.thread.is_alive())

    def test_http_endpoint_is_independent_of_engine_and_uses_security_guard(self):
        spec = importlib.util.spec_from_file_location('hardware_console', ROOT / 'scripts/local-console.py')
        console = importlib.util.module_from_spec(spec); spec.loader.exec_module(console)
        server = console.Server(('127.0.0.1', 0), console.Handler)
        server.hardware = SystemDiskSampler(lambda: None)
        server.control = SimpleNamespace(port=server.server_port)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/launcher/hardware'
            with urllib.request.urlopen(url) as response:
                value = json.load(response)
                self.assertEqual(value['scope'], 'system')
                self.assertEqual(value['source'], 'psutil')
            request = urllib.request.Request(url, headers={'Sec-Fetch-Site': 'cross-site'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown(); worker.join(); server.server_close()


if __name__ == '__main__':
    unittest.main()
