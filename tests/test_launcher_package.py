"""Console package integrity and portable archive metadata; no native build."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('linux_package', ROOT/'scripts/linux/package-console.py')
package=importlib.util.module_from_spec(spec); spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.chat=self.directory/'chat'; self.chat.mkdir()
        (self.chat/'index.html').write_text('<html>fixture</html>')
        (self.chat/'ui-build.json').write_text(json.dumps(dict(console_chat_protocol=1)))
        self.wheel=self.directory/'psutil-7.2.2-cp36-abi3-manylinux_2_28_x86_64.whl'
        with zipfile.ZipFile(self.wheel,'w') as archive:
            archive.writestr('psutil/__init__.py',"__version__='7.2.2'\n")
            archive.writestr('psutil-7.2.2.dist-info/licenses/LICENSE','fixture license')
        self.sha=hashlib.sha256(self.wheel.read_bytes()).hexdigest()

    def build(self, output='package', sha=None):
        return package.package(ROOT,self.directory/output,self.chat,self.wheel,sha or self.sha)

    def test_complete_manifest_lf_and_executable_tar_entries(self):
        result=self.build(); directory=Path(result['directory'])
        info=json.loads((directory/'console-build.json').read_text())
        self.assertEqual(info['supported_backends'],['ninfer'])
        self.assertFalse(info['native_worker_included'])
        self.assertNotIn(b'\r',(directory/'SHA256SUMS').read_bytes())
        for line in (directory/'SHA256SUMS').read_text().splitlines():
            sha,name=line.split('  ',1)
            self.assertEqual(hashlib.sha256((directory/name).read_bytes()).hexdigest(),sha)
        for name,expected in info['controller_files'].items():
            self.assertEqual(hashlib.sha256((directory/name).read_bytes()).hexdigest(),expected)
        with tarfile.open(result['archive']) as archive:
            for name in ('START-HERE-CN.sh','START-HERE-EN.sh','scripts/linux/start-ui.sh'):
                member=archive.getmember('package/'+name)
                self.assertEqual(member.mode,0o755)
                self.assertNotIn(b'\r',archive.extractfile(member).read())
            self.assertEqual(archive.getmember('package/scripts/local-console.py').mode,0o644)
        with self.assertRaisesRegex(ValueError,'already exists'): self.build()

    def test_bad_wheel_or_chat_cannot_create_a_package(self):
        with self.assertRaisesRegex(ValueError,'SHA-256'): self.build(sha='0'*64)
        self.assertFalse((self.directory/'package').exists())
        (self.chat/'ui-build.json').write_text('{"console_chat_protocol":0}')
        with self.assertRaisesRegex(ValueError,'chat'): self.build()

    def test_wheel_traversal_is_rejected_and_partial_folder_removed(self):
        with zipfile.ZipFile(self.wheel,'a') as archive:
            archive.writestr('../outside.py','malicious fixture')
        self.sha=hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'Unexpected wheel'): self.build()
        self.assertFalse((self.directory/'package').exists())
        self.assertFalse(list(self.directory.glob('.console-package-*')))
        self.assertFalse((self.directory/'outside.py').exists())


if __name__=='__main__': unittest.main()
