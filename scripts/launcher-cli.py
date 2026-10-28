#!/usr/bin/env python3
"""Inspect and export a structured KVMem configuration without starting the UI."""
import argparse
import json
import os
from pathlib import Path
import sys

from launcher.config import ROOT
from launcher.plan import build_plan
from launcher.export import export_profile, read_revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-root', type=Path, default=ROOT)
    parser.add_argument('--profiles-root', type=Path,
        default=Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'kvmem/profiles')
    actions = parser.add_subparsers(dest='action', required=True)
    preview = actions.add_parser('plan', help='Validate and print a redacted plan; do not load a model')
    preview.add_argument('--config', type=Path, required=True)
    generate = actions.add_parser('export', help='Save a Linux script and optional service template')
    generate.add_argument('--config', type=Path, required=True)
    generate.add_argument('--profile', required=True)
    generate.add_argument('--api-key-file', type=Path)
    generate.add_argument('--store-api-key', action='store_true', help='Store the supplied API key in a private local file')
    generate.add_argument('--scope', choices=('user', 'system'), default='user')
    generate.add_argument('--user', help='Ordinary runtime account for a system service')
    verify = actions.add_parser('verify', help='Verify a generated revision, including user modifications')
    verify.add_argument('--profile', required=True)
    verify.add_argument('--revision', required=True)
    service = actions.add_parser('service', help='Control only explicitly registered generated systemd services')
    service.add_argument('--action', dest='service_action', choices=('list', 'register', 'status', 'start', 'stop', 'restart', 'enable', 'disable', 'remove', 'logs'), required=True)
    service.add_argument('--profile')
    service.add_argument('--revision')
    service.add_argument('--scope', choices=('user', 'system'), default='user')
    args = parser.parse_args()
    try:
        if args.action == 'service':
            from launcher.services import Services
            manager = Services(args.profiles_root)
            if args.service_action == 'list':
                result = dict(capabilities=manager.available(), services=manager.list())
            elif args.service_action == 'register':
                result = manager.register(args.profile, args.revision, args.scope)
            elif args.service_action == 'status':
                result = manager.status(args.profile, args.scope)
            elif args.service_action == 'logs':
                result = manager.journal(args.profile, args.scope)
            else:
                result = manager.action(args.profile, args.scope, args.service_action)
        elif args.action == 'verify':
            _, _, result = read_revision(args.profiles_root, args.profile, args.revision)
        else:
            data = json.loads(args.config.read_text(encoding='utf-8-sig'))
            if args.action == 'plan':
                result = build_plan(data, args.backend_root)
            else:
                result = export_profile(data, args.backend_root, args.profiles_root, args.profile,
                    secret_file=args.api_key_file, persist_secret=args.store_api_key, scope=args.scope, user=args.user)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
