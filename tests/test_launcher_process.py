"""Real Linux process ownership tests, including abrupt controller death."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import test_launcher_export as export_fixtures
from launcher.process_linux import OwnedProcess
ROOT = export_fixtures.ROOT


def wait_for(check, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.03)
    raise AssertionError('Timed out waiting for process lifecycle evidence')


def running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


@unittest.skipIf(os.name == 'nt', 'Linux process groups and controller pipes')
class ProcessTest(unittest.TestCase):
    setUp = export_fixtures.ExportTest.setUp
    export = export_fixtures.ExportTest.export

    def test_owned_stop_delivers_termination_to_actual_worker(self):
        result = self.export()
        with (self.directory / 'worker.log').open('wb') as log:
            owned = OwnedProcess(result['script'], log,
                dict(os.environ, KVMEM_TEST_OUTPUT=str(self.output), KVMEM_TEST_HOLD='1'))
            try:
                wait_for(self.output.exists)
                self.assertEqual(json.loads(self.output.read_text())['pid'], owned.pid)
                self.assertIsNone(owned.poll())
            finally:
                owned.close()
            self.assertEqual(owned.wait(timeout=1), 0)
            self.assertFalse(running(owned.pid))
            self.assertEqual(self.output.with_suffix('.signal').read_text(), '15')

    def test_abrupt_controller_death_closes_pipe_and_cleans_worker(self):
        result = self.export()
        driver = '''import sys, time
sys.path.insert(0, sys.argv[1])
from launcher.process_linux import OwnedProcess
log = open(sys.argv[3], 'wb')
owned = OwnedProcess(sys.argv[2], log)
while True: time.sleep(1)
'''
        parent = subprocess.Popen([sys.executable, '-c', driver, str(ROOT / 'scripts'),
            result['script'], str(self.directory / 'parent.log')],
            env=dict(os.environ, KVMEM_TEST_OUTPUT=str(self.output), KVMEM_TEST_HOLD='1'))
        try:
            wait_for(self.output.exists)
            pid = json.loads(self.output.read_text())['pid']
            self.assertTrue(running(pid))
            parent.kill()
            parent.wait(timeout=3)
            wait_for(lambda: not running(pid))
            self.assertEqual(self.output.with_suffix('.signal').read_text(), '15')
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=3)

    def test_failed_worker_exit_does_not_become_success(self):
        result = self.export()
        with (self.directory / 'failed.log').open('wb') as log:
            owned = OwnedProcess(result['script'], log,
                dict(os.environ, KVMEM_TEST_OUTPUT=str(self.output), KVMEM_TEST_EXIT='42'))
            try:
                self.assertEqual(owned.wait(timeout=3), 42)
            finally:
                owned.close()


if __name__ == '__main__':
    unittest.main()
