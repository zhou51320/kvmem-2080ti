"""Bounded management of explicitly registered generated systemd services.

The browser never supplies a command, unit path, or sudo password. System units
are installed by an administrator in a terminal, then verified and registered.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

from .export import read_revision, _generation_lock
from .systemd import profile_id as validate_id, unit_name
from .worker import worker_digest


def journal_secret(plan):
    """Load a bounded credential before exposing journal output to any caller."""
    reference = plan.get('secret_ref')
    if not reference:
        return ''
    if reference.get('kind') != 'file':
        raise ValueError('A stored credential is required to redact background logs')
    try:
        with Path(reference['path']).open(encoding='utf-8') as stream:
            raw = stream.read(515)
        if len(raw) > 514:
            raise ValueError('Credential file is too large')
        key = raw.rstrip('\r\n')
        if not key or len(key) > 512 or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', key):
            raise ValueError('Invalid credential')
    except (OSError, ValueError, KeyError) as error:
        raise ValueError('Cannot read the service credential; journal output was withheld') from error
    return key


def redacted_journal(text, secrets, limit=262144):
    for secret in sorted(set(secrets) - {''}, key=len, reverse=True):
        text = text.replace(secret, '<redacted>')
    data = text.encode('utf-8')
    truncated = len(data) > limit
    if truncated:
        data = data[-limit:]
        # Drop a partial record, including a partial UTF-8 character or token.
        boundary = data.find(b'\n')
        data = data[boundary + 1:] if boundary >= 0 else b''
    return data.decode('utf-8'), truncated


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.service-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Services:
    def __init__(self, profiles_root):
        self.root = Path(profiles_root).resolve()

    @staticmethod
    def _run(argv, *, check=True, timeout=15):
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError('systemd command unavailable: ' + str(error)) from error
        if check and result.returncode:
            raise ValueError((result.stderr or result.stdout or 'systemd command failed').strip()[:4096])
        return result

    @staticmethod
    def _command(scope):
        if scope not in ('user', 'system'):
            raise ValueError('Service scope must be user or system')
        return ['systemctl', '--no-pager', '--no-ask-password'] + (['--user'] if scope == 'user' else [])

    def available(self):
        if os.name == 'nt' or not shutil.which('systemctl') or not Path('/run/systemd/system').is_dir():
            return dict(available=False, user=False, system=False, reason='systemd is not running; independent scripts remain available')
        user = self._run(self._command('user') + ['show', '--property=Version'], check=False)
        linger = self._run(['loginctl', 'show-user', str(os.getuid()), '--property=Linger', '--value'], check=False)
        return dict(available=True, user=user.returncode == 0, system=True,
                    linger=linger.stdout.strip() == 'yes',
                    reason=user.stderr.strip()[:512] if user.returncode else None)

    def _record_path(self, profile, scope):
        validate_id(profile); self._command(scope)
        return self.root / profile / ('service-' + scope + '.json')

    def _unit_path(self, profile, scope):
        self._command(scope)
        base = (Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd/user'
                if scope == 'user' else Path('/etc/systemd/system'))
        return base / unit_name(profile)

    def read(self, profile, scope):
        path = self._record_path(profile, scope)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('Service registration must not be a symlink')
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('schema_version') != 1 or value.get('profile_id') != profile or value.get('scope') != scope:
            raise ValueError('Invalid service registration')
        directory, manifest, plan = read_revision(self.root, profile, value['revision'])
        if manifest['scope'] != scope or value.get('unit_sha256') != manifest['files'][unit_name(profile)]:
            raise ValueError('Service registration does not match its generated version')
        return value, directory, manifest, plan

    def _show(self, profile, scope):
        properties = ['LoadState', 'ActiveState', 'SubState', 'MainPID', 'ExecMainStatus',
                      'FragmentPath', 'DropInPaths', 'UnitFileState', 'InvocationID', 'Result', 'NeedDaemonReload']
        run = self._run(self._command(scope) + ['show', unit_name(profile),
                         '--property=' + ','.join(properties)])
        return dict(line.split('=', 1) for line in run.stdout.splitlines() if '=' in line)

    def _verify_unit(self, profile, scope, digest, state):
        path = self._unit_path(profile, scope)
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Installed unit was changed or is missing; refusing to control it')
        if (state.get('FragmentPath') != str(path) or state.get('DropInPaths')
                or state.get('NeedDaemonReload') == 'yes' or state.get('LoadState') != 'loaded'):
            raise ValueError('Loaded systemd unit differs from its registration; inspect it in a terminal')

    def status(self, profile, scope):
        value, _, _, plan = self.read(profile, scope)
        state = self._show(profile, scope)
        self._verify_unit(profile, scope, value['unit_sha256'], state)
        pid = int(state.get('MainPID') or 0)
        # Health on a port alone cannot establish ownership. Check the actual
        # main process against the planned worker, including its arguments.
        matched = False
        identity_error = None
        if pid:
            try:
                argv = Path(f'/proc/{pid}/cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
                expected = list(plan['argv'])
                # Interpreter scripts are useful in host tests. Native workers
                # are compared using argv and the recorded executable digest.
                interpreted = len(argv) == len(expected) + 1 and argv[1] == expected[0]
                if interpreted:
                    argv = argv[1:]
                if '--api-key' in expected:
                    index = expected.index('--api-key') + 1
                    if len(argv) > index:
                        argv[index] = '<redacted>'
                matched = argv == expected
                if matched and not interpreted:
                    matched = os.path.samefile(f'/proc/{pid}/exe', expected[0])
                worker_hash = plan.get('worker', {}).get('executable_sha256')
                if matched and worker_hash:
                    matched = worker_digest(expected[0]) == worker_hash
            except (OSError, ValueError, UnicodeError) as error:
                identity_error = str(error)
        return dict(profile_id=profile, revision=value['revision'], scope=scope, unit=unit_name(profile),
                    active=state.get('ActiveState'), substate=state.get('SubState'), pid=pid or None,
                    enabled=state.get('UnitFileState') in ('enabled', 'enabled-runtime'),
                    invocation=state.get('InvocationID', ''), result=state.get('Result'),
                    exit_code=int(state.get('ExecMainStatus') or 0), worker_matches=matched,
                    identity_error=identity_error)

    def register(self, profile, revision, scope='user'):
        directory, manifest, plan = read_revision(self.root, profile, revision)
        if scope != manifest['scope']:
            raise ValueError('Choose the scope used to generate this version')
        if (plan.get('secret_ref') or {}).get('kind') == 'environment':
            raise ValueError('A background service needs a readable API key file; explicitly store the key or supply a file before export')
        record_path = self._record_path(profile, scope)
        target = self._unit_path(profile, scope)
        digest = manifest['files'][unit_name(profile)]
        with _generation_lock(directory.parent.parent):
            previous_bytes = None
            if record_path.exists():
                previous = (self.status(profile, scope) if scope == 'user' else
                            dict(revision=self.read(profile, scope)[0]['revision'],
                                 active=self._show(profile, scope).get('ActiveState')))
                if previous['revision'] != revision and previous['active'] in ('active', 'activating', 'deactivating'):
                    raise ValueError('Stop this service before registering a different version; its current script remains unchanged')
                if scope == 'user':
                    previous_bytes = target.read_bytes()
            elif target.exists() or target.is_symlink():
                if scope == 'user':
                    raise ValueError('An unregistered unit already has this name; choose another profile ID')
            if scope == 'user':
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor, temporary = tempfile.mkstemp(prefix='.kvmem-', dir=target.parent)
                try:
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write((directory / unit_name(profile)).read_bytes())
                        stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
            try:
                if scope == 'user':
                    self._run(self._command(scope) + ['daemon-reload'])
                state = self._show(profile, scope)
                self._verify_unit(profile, scope, digest, state)
                atomic_json(record_path, dict(schema_version=1, profile_id=profile, revision=revision,
                                             scope=scope, unit_sha256=digest))
            except BaseException:
                # Restore only the exact bytes installed by this operation.
                # A concurrent manual edit belongs to the user and is retained.
                if scope == 'user' and target.is_file() and not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                    if previous_bytes is None:
                        target.unlink()
                    else:
                        descriptor, temporary = tempfile.mkstemp(prefix='.kvmem-restore-', dir=target.parent)
                        try:
                            with os.fdopen(descriptor, 'wb') as stream:
                                stream.write(previous_bytes); stream.flush(); os.fsync(stream.fileno())
                            os.replace(temporary, target)
                        finally:
                            if os.path.exists(temporary): os.unlink(temporary)
                    try: self._run(self._command(scope) + ['daemon-reload'], check=False)
                    except ValueError: pass
                raise
        return self.status(profile, scope)

    def action(self, profile, scope, action):
        if action not in ('start', 'stop', 'restart', 'enable', 'disable', 'remove'):
            raise ValueError('Unsupported service action')
        state = self.status(profile, scope)
        if action in ('stop', 'restart'):
            self._remember_invocation(profile, scope, state)
        if action == 'remove':
            if scope != 'user':
                raise ValueError('Remove a system unit in an administrator terminal')
            if state['active'] not in ('inactive', 'failed') or state['enabled']:
                raise ValueError('Stop and disable the service before removing its registration')
            self._unit_path(profile, scope).unlink()
            self._run(self._command(scope) + ['daemon-reload'])
            self._record_path(profile, scope).unlink()
            return dict(removed=True)
        self._run(self._command(scope) + [action, unit_name(profile)], timeout=75 if action == 'stop' else 30)
        return self.status(profile, scope)

    def _remember_invocation(self, profile, scope, state):
        invocation = state.get('invocation', '')
        if not re.fullmatch(r'[0-9a-f]{32}', invocation):
            return
        with _generation_lock(self.root / profile):
            value, _, _, _ = self.read(profile, scope)
            if value['revision'] == state['revision'] and value.get('last_invocation') != invocation:
                value['last_invocation'] = invocation
                atomic_json(self._record_path(profile, scope), value)

    def journal(self, profile, scope, *, redactions=(), scoring=False):
        state = self.status(profile, scope)
        value, _, _, plan = self.read(profile, scope)
        if value['revision'] != state['revision']:
            raise ValueError('Service registration changed while reading logs; retry')
        secrets = (*redactions, journal_secret(plan))
        invocation = state['invocation']
        if invocation:
            self._remember_invocation(profile, scope, state)
        else:
            invocation = value.get('last_invocation', '')
        if not re.fullmatch(r'[0-9a-f]{32}', invocation):
            return dict(text='', available=False, session='', running=False, truncated=False)
        # systemd 249 seeks to the tail before filtering in forward mode.
        # Reverse iteration scans backwards until it finds matching reports.
        options = (['--reverse', '--lines=100', '--grep=KVMEM_(SCORING|LIMITS|BLOCKS) ']
                   if scoring else ['--lines=1000'])
        run = self._run(['journalctl', '--no-pager', '--output=cat'] + options
                        + (['--user'] if scope == 'user' else [])
                        + ['--unit=' + unit_name(profile), '_SYSTEMD_INVOCATION_ID=' + invocation], check=False)
        output = run.stdout
        if scoring and output:
            # Parse reports in chronological order: the newest values win.
            output = '\n'.join(reversed(output.splitlines())) + '\n'
        text, truncated = redacted_journal(output, secrets)
        error, _ = redacted_journal(run.stderr, secrets, 512)
        return dict(text=text, available=run.returncode == 0, session=invocation,
                    truncated=truncated,
                    running=state['active'] in ('active', 'activating'),
                    error=error.strip() if run.returncode else None)

    def list(self):
        result = []
        if self.root.exists():
            for profile in sorted(self.root.iterdir()):
                if not profile.is_dir() or profile.is_symlink(): continue
                for scope in ('user', 'system'):
                    if not (profile / ('service-' + scope + '.json')).is_file(): continue
                    try:
                        result.append(self.status(profile.name, scope))
                    except (OSError, ValueError) as error:
                        result.append(dict(profile_id=profile.name, scope=scope, error=str(error)))
        return result
