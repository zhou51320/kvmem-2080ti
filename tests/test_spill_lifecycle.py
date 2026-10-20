"""Public spill-file cleanup on normal exit and forced partial-write termination."""
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

WORKER = str(Path(sys.argv.pop(1)).resolve())


class SpillLifecycleTests(unittest.TestCase):
    def test_exit_and_termination(self):
        for mode in ('hold', 'partial'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sentinel = root / 'user-file'
                sentinel.write_text('keep', encoding='utf-8')
                process = subprocess.Popen([WORKER, str(root), mode], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, encoding='utf-8')
                try:
                    lines = queue.Queue()
                    reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
                    reader.start()
                    process.stdin.write('start\n')
                    process.stdin.flush()
                    self.assertEqual(lines.get(timeout=15).strip(), 'READY')
                    if mode == 'partial':
                        process.kill()
                    else:
                        process.stdin.write('quit\n')
                        process.stdin.flush()
                    process.wait(timeout=15)
                    if mode == 'hold':
                        self.assertEqual(process.returncode, 0, process.stderr.read())
                    reader.join(timeout=1)
                    self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
                    self.assertEqual(list(root.iterdir()), [sentinel], 'spill file survived process exit')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=15)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()


if __name__ == '__main__':
    unittest.main()
