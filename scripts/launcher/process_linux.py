"""Own a temporary worker group, including cleanup when its controller dies.

The supervisor alone inherits the control-pipe read end. Workers must never
inherit either control endpoint, or parent death would not produce EOF.
"""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


class OwnedProcess:
    def __init__(self, script, log, env=None):
        read_fd, self.control_fd = os.pipe()
        status_read, status_write = os.pipe()
        self.supervisor = None
        try:
            self.supervisor = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '--supervise',
                 str(read_fd), str(status_write), str(script)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                close_fds=True, pass_fds=(read_fd, status_write), start_new_session=True)
        finally:
            os.close(read_fd)
            os.close(status_write)
            if self.supervisor is None:
                os.close(self.control_fd)
                self.control_fd = None
                os.close(status_read)
        try:
            if not select.select([status_read], [], [], 10)[0]:
                raise RuntimeError('Linux supervisor did not start the worker')
            message = os.read(status_read, 4096)
            status = json.loads(message)
            if 'error' in status:
                raise RuntimeError(status['error'])
            self.pid = int(status['pid'])
            self.start_time = str(status['start_time'])
        except BaseException:
            self.close()
            raise
        finally:
            os.close(status_read)

    @property
    def returncode(self):
        return self.supervisor.returncode

    def poll(self):
        return self.supervisor.poll()

    def wait(self, timeout=None):
        return self.supervisor.wait(timeout=timeout)

    def close(self):
        if self.control_fd is not None:
            os.close(self.control_fd)
            self.control_fd = None
        if self.supervisor:
            try:
                self.supervisor.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.supervisor.terminate()
                self.supervisor.wait(timeout=12)


def _start_time(pid):
    # comm can contain spaces and closing parentheses. Fields after its final
    # ')' start with field 3; starttime is field 22.
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def _group_signal(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def supervise(read_fd, status_fd, script):
    stop = False
    child = None

    def request_stop(*_):
        nonlocal stop
        stop = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, request_stop)
    try:
        child = subprocess.Popen([script], stdin=subprocess.DEVNULL,
                                 close_fds=True, start_new_session=True)
        os.write(status_fd, json.dumps({'pid': child.pid, 'start_time': _start_time(child.pid)}).encode())
        os.close(status_fd)
        status_fd = None
        while not stop:
            # Observe completion without reaping: retain the group leader's PID
            # until cleanup has begun, so it cannot be reused by another worker.
            ended = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if ended is not None:
                break
            if select.select([read_fd], [], [], .1)[0] and not os.read(read_fd, 4096):
                stop = True
        _group_signal(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if result is not None:
                # Leader remains unreaped while its remaining children are
                # terminated. Descendants share this unique process group.
                _group_signal(child.pid, signal.SIGKILL)
                return child.wait()
            time.sleep(.05)
        _group_signal(child.pid, signal.SIGKILL)
        return child.wait(timeout=5)
    except BaseException as error:
        if status_fd is not None:
            try:
                os.write(status_fd, json.dumps({'error': str(error)}).encode())
            except OSError:
                pass
        if child is not None:
            _group_signal(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        print('Linux supervisor: ' + str(error), file=sys.stderr)
        return 1
    finally:
        os.close(read_fd)
        if status_fd is not None:
            os.close(status_fd)


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--supervise':
        raise SystemExit('This internal supervisor requires a controller pipe')
    code = supervise(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    raise SystemExit(code if code >= 0 else 128 - code)
