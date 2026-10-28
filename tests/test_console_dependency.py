import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parents[1] / 'scripts/install-console-dependency.py')
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)


class DependencyTests(unittest.TestCase):
    def test_verified_offline_install_preserves_license_and_embedded_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); wheel=root/'psutil-7.2.2-cp37-abi3-win_amd64.whl'
            runtime=root/'runtime';runtime.mkdir()
            paths=runtime/'python312._pth';paths.write_text('python312.zip\n.\n')
            with zipfile.ZipFile(wheel,'w') as archive:
                archive.writestr('psutil/__init__.py','')
                archive.writestr('psutil-7.2.2.dist-info/licenses/LICENSE','license fixture')
            digest=hashlib.sha256(wheel.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'SHA-256'):
                installer.install(wheel,'0'*64,runtime)
            self.assertFalse((runtime/'Lib').exists())
            installer.install(wheel,digest,runtime)
            self.assertIn('Lib/site-packages',paths.read_text().splitlines())
            self.assertEqual((runtime/'Lib/site-packages/psutil-7.2.2.dist-info/licenses/LICENSE').read_text(),'license fixture')
            with self.assertRaisesRegex(ValueError,'replace'):
                installer.install(wheel,digest,runtime)

    def test_bad_paths_are_rejected_before_writing(self):
        for name in ('../escape','/absolute','psutil/../../escape','psutil/C:escape','other/file'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);wheel=root/'psutil-7.2.2-cp37-abi3-win_amd64.whl'
                with zipfile.ZipFile(wheel,'w') as archive: archive.writestr(name,'bad')
                with self.assertRaisesRegex(ValueError,'wheel path'):
                    installer.install(wheel,hashlib.sha256(wheel.read_bytes()).hexdigest(),root/'runtime')
                self.assertFalse((root/'runtime').exists())


if __name__=='__main__': unittest.main()
