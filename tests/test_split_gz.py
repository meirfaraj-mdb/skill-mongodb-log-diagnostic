import gzip
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('split_gz', Path(__file__).resolve().parents[1] / 'scripts' / 'split_gz.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class SplitTests(unittest.TestCase):
    def check_roundtrip(self, data, count):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'mongodb.log.gz'
            source.write_bytes(gzip.compress(data))
            original = source.read_bytes()
            files = module.split_gz(source, part_size=8)
            self.assertEqual(len(files), count)
            self.assertNotEqual(files[-1].suffix, '.gz')
            self.assertTrue(all(f.suffix == '.gz' for f in files[:-1]))
            decoded = [gzip.decompress(f.read_bytes()) for f in files[:-1]] + [files[-1].read_bytes()]
            self.assertEqual(b''.join(decoded), data)
            self.assertTrue(all(len(part) == 8 for part in decoded[:-1]))
            self.assertEqual(source.read_bytes(), original)
    def test_empty(self): self.check_roundtrip(b'', 1)
    def test_small(self): self.check_roundtrip(b'abc', 1)
    def test_exact(self): self.check_roundtrip(b'x' * 16, 2)
    def test_remainder(self): self.check_roundtrip(bytes(range(25)), 4)
    def test_corrupt_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bad.gz'
            source.write_bytes(gzip.compress(b'hello' * 8)[:-5])
            with self.assertRaises((OSError, EOFError)):
                module.split_gz(source, part_size=8)
            self.assertEqual(list(Path(directory).iterdir()), [source])
    def test_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'log.gz'
            source.write_bytes(gzip.compress(b'abc'))
            module.split_gz(source, part_size=8)
            with self.assertRaises(FileExistsError): module.split_gz(source, part_size=8)

if __name__ == '__main__': unittest.main()
