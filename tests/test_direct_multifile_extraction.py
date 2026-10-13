import gzip
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / 'skills/mongodb-log-diagnostic/scripts/extract_mongodb_logs.py'
spec = importlib.util.spec_from_file_location('multi', WRAPPER)
multi = importlib.util.module_from_spec(spec); spec.loader.exec_module(multi)

class Tests(unittest.TestCase):
    def test_split_utf8_and_missing_newline(self):
        data = 'first café\nsecond record'.encode()
        cut = data.index(b'\xc3') + 1
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp)/'x.part-00001.gz'; a.write_bytes(gzip.compress(data[:cut]))
            b = Path(tmp)/'x.part-00002'; b.write_bytes(data[cut:])
            reader = multi.MultiFileLines(multi.input_sequences([b, a]))
            self.assertEqual(list(reader(a))+list(reader(b)), ['first café\n', 'second record'])
    def test_independent_logs_do_not_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp)/'a.log'; a.write_bytes(b'first')
            b = Path(tmp)/'b.log.gz'; b.write_bytes(gzip.compress(b'second\n'))
            reader = multi.MultiFileLines(multi.input_sequences([a,b]))
            self.assertEqual(list(reader(a))+list(reader(b)), ['first','second\n'])
    def test_missing_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp)/f'x.part-{n}' for n in (1,3)]
            for p in paths: p.touch()
            with self.assertRaises(ValueError): multi.input_sequences(paths)
    def test_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'x.log'; p.touch()
            with self.assertRaises(ValueError): multi.input_sequences([p,p])
    def test_original_plus_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=[Path(tmp)/n for n in ('x.log.gz','x.log.part-1.gz')]
            for p in paths: p.touch()
            with self.assertRaises(ValueError): multi.input_sequences(paths)
    def test_manifest_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'manifest.json'; p.write_text('[]')
            with self.assertRaises(ValueError): multi.main(['--input-manifest',str(p)])
    def test_one_analyzer_run_shared_state(self):
        fixture = '''
import argparse
from pathlib import Path
calls = 0
def parse_args():
    p=argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('inputs', nargs='+', type=Path)
    return p.parse_args()
def read_lines(path):
    raise AssertionError('adapter not installed')
def main():
    global calls
    calls += 1
    args=parse_args()
    lines=[]
    for path in args.inputs:
        lines.extend(read_lines(path))
    args.output.write_text(''.join(lines))
    assert calls == 1
    return 0
'''
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            (d/'extract_mongodb_log.py').write_text(fixture)
            a=d/'a.part-1.gz'; a.write_bytes(gzip.compress(b'{"one":'))
            b=d/'a.part-2'; b.write_bytes(b'1}\n')
            manifest=d/'inputs.json'; manifest.write_text(json.dumps([str(b),str(a)]))
            output=d/'output'
            old=sys.argv[:]
            with patch.object(multi,'__file__',str(d/'extract_mongodb_logs.py')):
                self.assertEqual(multi.main(['--input-manifest',str(manifest),'--output',str(output)]),0)
            self.assertEqual(output.read_text(),'{"one":1}\n')
            self.assertEqual(sys.argv,old)
    def test_stage_downloads_original_files(self):
        # Reuse the previous grouping test's stubbed stage dependencies.
        old_tests=Path(__file__).with_name('stage_test_support.py')
        setup=old_tests.read_text().split('class Tests')[0]
        setup=setup.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(ROOT)!r})")
        namespace={'__file__':str(old_tests)}
        exec(compile(setup,str(old_tests),'exec'),namespace)
        stage=namespace['m']; Store=namespace['Store']
        data={'a.part-1.gz':gzip.compress(b'partial'), 'a.part-2':b'record\n'}
        with tempfile.TemporaryDirectory() as tmp:
            manifest=stage.download_inputs(Store(data),list(data),Path(tmp))
            paths=[Path(p) for p in json.loads(manifest.read_text())]
            self.assertEqual([p.read_bytes() for p in paths],list(data.values()))
            self.assertFalse((Path(tmp)/'node-input.gz').exists())

if __name__ == '__main__': unittest.main()
