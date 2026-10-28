"""systemd unit rendering; installation and lifecycle controls are separate."""
from pathlib import Path
import re


def profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise ValueError('Profile ID must be 1..64 lowercase letters, digits, underscores or hyphens')
    return value


def unit_name(value):
    return 'kvmem-' + profile_id(value) + '.service'


def _quote(value, *, command=False):
    """systemd quoting is not shell quoting. Escape specifiers separately."""
    value = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Control characters cannot be written to a systemd unit')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def render_unit(plan, script, *, scope='user', user=None):
    if scope not in ('user', 'system'):
        raise ValueError('Service scope must be user or system')
    if scope == 'system' and (not isinstance(user, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', user)):
        raise ValueError('System services require an explicit ordinary runtime user')
    if scope == 'system' and user == 'root':
        raise ValueError('Choose an ordinary runtime user for inference')
    script = Path(script)
    if not script.is_absolute() or not Path(plan['cwd']).is_absolute():
        raise ValueError('Service paths must be absolute')
    cwd = str(plan['cwd'])
    if cwd != cwd.strip() or any(ord(c) < 32 or ord(c) == 127 for c in cwd):
        raise ValueError('WorkingDirectory contains unsupported whitespace')
    mounts = [str(script.parent), plan['cwd']]
    model = plan['effective_parameters'].get('Model')
    if model:
        mounts.append(str(Path(model).parent))
    lines = ['[Unit]', 'Description=KVMem inference service']
    # User managers cannot order system mount units; missing user paths fail preflight.
    if scope == 'system':
        lines.append('RequiresMountsFor=' + ' '.join(_quote(x) for x in dict.fromkeys(mounts)))
    lines += ['StartLimitIntervalSec=300', 'StartLimitBurst=3', '', '[Service]', 'Type=exec']
    if scope == 'system':
        lines.append('User=' + user)
    # WorkingDirectory is one path, not a word list. systemd 249 treats quotes
    # as literal characters here; internal spaces are already part of the path.
    lines += ['WorkingDirectory=' + cwd.replace('%', '%%'), 'ExecStart=' + _quote(script, command=True),
              'Restart=on-failure', 'RestartSec=10', 'RestartPreventExitStatus=75 78',
              'KillMode=control-group', 'TimeoutStopSec=60', 'StandardOutput=journal',
              'StandardError=journal', 'UMask=0077', '', '[Install]',
              'WantedBy=' + ('default.target' if scope == 'user' else 'multi-user.target'), '']
    return '\n'.join(lines)
