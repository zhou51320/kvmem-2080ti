"""Portable launch contract: independent invariants and legacy CLI comparisons."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from launcher.plan import build_plan, token_settings
from launcher.worker import probe_worker

GPU = 'GPU-00000000-0000-0000-0000-000000000001'


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Hosted Windows TEMP may use an 8.3 alias (RUNNER~1). Use one path
        # spelling for exact argv comparisons with PowerShell's FullName.
        self.directory = Path(self.tmp.name).resolve()
        self.worker = self.directory / 'worker with spaces.exe'
        self.worker.write_text('#!/bin/sh\nexit 0\n')
        self.worker.chmod(0o755)
        self.models = {}
        for backend, extension in [('ninfer', 'ninfer'), ('llamacpp', 'gguf')]:
            path = self.directory / ('model with spaces.' + extension)
            path.touch()
            self.models[backend] = path

    def config(self, backend='ninfer', **values):
        return {'schema_version': 1, 'parameters': dict(Backend=backend,
            Model=str(self.models[backend]), Worker=str(self.worker), Gpu=GPU, **values)}

    def plan(self, backend='ninfer', **values):
        return build_plan(self.config(backend, **values))

    def test_token_invariants_cover_page_boundaries(self):
        for backend in ('ninfer', 'llamacpp', 'polystrata'):
            for reserve in (128, 129, 255, 256, 1023, 1024):
                values = dict(Budget=1099, Reserve=reserve, SinkTokens=65,
                              RecentTokens=66, Prefill=517, Context=4096)
                plan = token_settings(backend, values)
                p, block = plan['effective'], plan['block_tokens']
                for key in ('Budget', 'Reserve', 'SinkTokens', 'RecentTokens'):
                    self.assertEqual(p[key] % block, 0)
                self.assertLessEqual(p['SinkTokens'] + p['RecentTokens'], p['Budget'])
                self.assertLessEqual(p['Budget'] + p['Reserve'], p['Context'])
                if backend != 'llamacpp':
                    self.assertLessEqual(p['Prefill'], p['Reserve'])
                if backend == 'ninfer':
                    self.assertEqual(p['Prefill'] % 128, 0)

    def test_capability_probe_and_digest_must_describe_the_same_worker(self):
        capability = dict(schema_version=1, backend='ninfer', query_scoring_revision=2,
                          independent_graph_lrfu=True, signed_lrfu_hits=True)
        def replace_during_probe(*args, **kwargs):
            replacement = self.worker.with_name('replacement.exe')
            replacement.write_text('a different worker without this contract\n')
            os.replace(replacement, self.worker)
            return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(capability), stderr='')
        with patch('launcher.worker.subprocess.run', side_effect=replace_during_probe):
            with self.assertRaisesRegex(ValueError, 'Worker changed'):
                probe_worker(self.worker, 'ninfer')

    def test_zero_native_prefill_sentinel_is_explicit(self):
        result = token_settings('ninfer', dict(Budget=1024, Reserve=512, SinkTokens=0,
                                              RecentTokens=64, Prefill=0, Context=2048))
        self.assertEqual(result['effective']['Prefill'], 512)
        self.assertEqual(result['effective']['SinkTokens'], 64)

    def test_invalid_token_capacity_and_small_reserve_fail(self):
        for values in (dict(Budget=128, Reserve=128, SinkTokens=128, RecentTokens=64),
                       dict(Budget=1024, Reserve=1024, Context=1024),
                       dict(Reserve=64), dict(RecentTokens=0)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.plan(**values)

    def test_effective_token_values_are_the_only_emitted_values(self):
        result = self.plan(Budget=1296, Reserve=385, Prefill=257,
                           SinkTokens=65, RecentTokens=64, Context=2048)
        args = result['argv']
        for flag, expected in [('--kvmem-budget', '1280'), ('--kvmem-gen-reserve', '384'),
                               ('--prefill-chunk', '256'), ('--kvmem-sink-tokens', '64')]:
            self.assertEqual(args.count(flag), 1)
            self.assertEqual(args[args.index(flag) + 1], expected)
        self.assertEqual(result['requested_parameters']['Budget'], 1296)

    def test_unlimited_output_maps_to_native_semantics(self):
        self.assertNotIn('--default-max-tokens', self.plan(MaxTokens=0)['argv'])
        args = self.plan('llamacpp', MaxTokens=0)['argv']
        self.assertEqual(args[args.index('-n') + 1], '-1')

    def test_query_controls_are_independent_and_have_explicit_defaults(self):
        args = self.plan()['argv']
        self.assertIn('--no-kvmem-lrfu', args)
        self.assertEqual(args[args.index('--kvmem-graph-alpha') + 1], '0.2')
        args = self.plan(QueryScorer='meanq-cosine', GraphAlpha=.35, Lrfu=False,
                         ScoreAggregation='mean', QueryMaxTokens=128, QueryReplayMaxTokens=1024)['argv']
        for flag, value in [('--kvmem-graph-alpha', '0.35'), ('--kvmem-score-agg', 'mean'),
                            ('--kvmem-query-max-tokens', '128'), ('--kvmem-query-replay-max-tokens', '1024')]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn('--no-kvmem-lrfu', args)

    def test_speculative_capability_boundaries(self):
        for backend, values in [('ninfer', dict(SpeculativeMode='dflash2', DraftPolicy='adaptive')),
                                ('llamacpp', dict(SpeculativeMode='mtp', DraftTokens=6)),
                                ('ninfer', dict(SpeculativeMode='none', NgramDrafts=5)),
                                ('llamacpp', dict(SpeculativeMode='dflash2')),
                                ('ninfer', dict(MtpDrafts=4, DraftTokens=3))]:
            with self.subTest(backend=backend, values=values), self.assertRaises(ValueError):
                self.plan(backend, **values)

    def test_concurrent_ngram_limit_preserves_other_settings(self):
        plan = self.plan(SpeculativeMode='mtp', DraftTokens=4, DraftPolicy='adaptive',
                         NgramDrafts=31, Concurrency=2)
        args = plan['argv']
        self.assertEqual(args[args.index('--ngram-draft-tokens') + 1], '15')
        self.assertEqual(plan['requested_parameters']['NgramDrafts'], 31)
        self.assertEqual(plan['effective_parameters']['NgramDrafts'], 15)
        self.assertIn('--adaptive-mtp', args)

    def test_multigpu_stage_order_and_identity(self):
        data = self.config(StageLayers=[10, 20])
        data['parameters']['Gpu'] = GPU + ',GPU-00000000-0000-0000-0000-000000000002'
        plan = build_plan(data)
        self.assertEqual(plan['environment']['CUDA_VISIBLE_DEVICES'], data['parameters']['Gpu'])
        self.assertEqual(plan['argv'][plan['argv'].index('--devices') + 1], '0,1')
        data['parameters']['Gpu'] = GPU + ',' + GPU
        with self.assertRaises(ValueError):
            build_plan(data)

    def test_mixed_quantized_kv_and_literal_metadata(self):
        alias = 'model "quoted" $x `echo forbidden` ; 中文'
        plan = self.plan('llamacpp', KvTypeK='q8', KvTypeV='q4', ModelAlias=alias,
                         PreserveThinking=False)
        self.assertEqual(plan['argv'][plan['argv'].index('--alias') + 1], alias)
        self.assertEqual(plan['argv'][plan['argv'].index('--cache-type-k') + 1], 'q8_0')
        self.assertEqual(plan['argv'][plan['argv'].index('--cache-type-v') + 1], 'q4_0')
        with self.assertRaises(ValueError):
            self.plan('llamacpp', KvTypeK='q8', KvTypeV='f16')

    def test_secret_redaction_and_private_plan(self):
        data = self.config(ApiKey='test-secret-value')
        public = build_plan(data)
        self.assertNotIn('test-secret-value', json.dumps(public))
        private = build_plan(data, reveal_secrets=True)
        self.assertEqual(private['argv'][private['argv'].index('--api-key') + 1], 'test-secret-value')
        self.assertNotIn('ApiKey', private['effective_parameters'])

    def test_native_escape_hatch_is_bounded(self):
        for args in (['--threads', '4; echo bad'], ['--host', '0.0.0.0'],
                     ['--threads', '4', '--threads', '8'], ['--threads']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.plan(WorkerArgs=args)
        args = self.plan(WorkerArgs=['--threads', '8'])['argv']
        self.assertEqual(args[-2:], ['--threads', '8'])

    @unittest.skipUnless(os.name == 'nt', 'PowerShell regression comparison')
    def test_windows_canonical_cli_equivalence(self):
        cases = [('ninfer', {}), ('llamacpp', {}),
                 ('ninfer', dict(SpeculativeMode='mtp', DraftTokens=4, DraftPolicy='adaptive',
                                 NgramDrafts=31, Concurrency=2, MaxTokens=0)),
                 ('ninfer', dict(SpeculativeMode='dflash2', DraftTokens=5, Vision=True,
                                 VisionResidency='cpu', PreserveThinking=True)),
                 ('ninfer', dict(Budget=1296, Reserve=385, Prefill=257, SinkTokens=65,
                                 RecentTokens=64, Context=2048)),
                 ('llamacpp', dict(KvTypeK='q8_0', KvTypeV='q4_0', MaxTokens=0,
                                   ThinkingBudget=0, PreserveThinking=False, Concurrency=2)),
                 ('llamacpp', dict(SpeculativeMode='mtp', DraftTokens=3, ReasoningEffort='high'))]
        for backend, values in cases:
            with self.subTest(backend=backend, values=values):
                data = self.config(backend, **values)
                path = self.directory / 'config.json'
                path.write_text(json.dumps(data['parameters']), encoding='utf-8')
                run = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                    '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'scripts/windows/describe-launch-json.ps1'),
                    '-BackendRoot', str(ROOT), '-ConfigFile', str(path)], capture_output=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr.decode('utf-8', 'replace'))
                output = run.stdout.decode('utf-8-sig')
                old = json.loads(output[output.index('{'):])
                new = build_plan(data)
                self.assertEqual(new['argv'], old['argv'])
                self.assertEqual(new['token_settings'], old['token_settings'])
                self.assertEqual(new['environment'], old['environment'])


if __name__ == '__main__':
    unittest.main()
