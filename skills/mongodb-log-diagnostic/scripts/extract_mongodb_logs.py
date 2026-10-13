#!/usr/bin/env python3
"""Run the existing MongoDB analyzer once over a node's original input files.

Supports --input-manifest JSON to avoid command-line size limits. Numbered
.part-N[.gz] files are byte-contiguous pieces, not independent log streams.
"""
from __future__ import annotations
import argparse
import gzip
import json
import re
import runpy
import sys
from pathlib import Path

PART = re.compile(r"^(.*)\.part-(\d+)(?:\.gz)?$")


def natural(value):
    return tuple((1, int(s)) if s.isdigit() else (0, s.lower())
                 for s in re.split(r"(\d+)", str(value)))


def input_sequences(paths):
    groups = {}
    seen = set()
    for path in paths:
        path = Path(path).resolve()
        if path in seen:
            raise ValueError(f"Duplicate input file: {path}")
        seen.add(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        match = PART.fullmatch(path.name)
        base = match.group(1) if match else path.name
        token = (path.parent, base, bool(match))
        groups.setdefault(token, []).append((int(match.group(2)) if match else 0, path))
    sequences = []
    for (parent, base, split), pieces in sorted(groups.items(), key=lambda item: natural(item[0])):
        pieces.sort()
        if split:
            numbers = [n for n, _ in pieces]
            if numbers != list(range(1, len(pieces) + 1)):
                raise ValueError(f"Missing or duplicate numbered part for {base}")
            if parent / base in seen or parent / (base + '.gz') in seen:
                raise ValueError(f"Original and split parts both supplied for {base}")
        sequences.append([path for _, path in pieces])
    return sequences


class MultiFileLines:
    """One-record buffering; reconstruct bytes before decoding UTF-8.

Source provenance for a cross-part record is the part where the record ends.
The original analyzer calls this once per path, in the planned order.
"""
    def __init__(self, sequences):
        self.order = [p for sequence in sequences for p in sequence]
        self.last = {sequence[-1] for sequence in sequences}
        self.position = 0
        self.pending = bytearray()

    def __call__(self, path):
        path = Path(path).resolve()
        if self.position >= len(self.order) or path != self.order[self.position]:
            raise ValueError('Analyzer input order differs from the multi-file plan')
        self.position += 1
        opener = gzip.open if path.name.lower().endswith('.gz') else open
        with opener(path, 'rb') as incoming:
            for fragment in incoming:
                if self.pending:
                    self.pending.extend(fragment)
                    if fragment.endswith(b'\n'):
                        yield self.pending.decode('utf-8', errors='replace')
                        self.pending.clear()
                elif fragment.endswith(b'\n'):
                    yield fragment.decode('utf-8', errors='replace')
                else:
                    self.pending.extend(fragment)
        if path in self.last and self.pending:
            yield self.pending.decode('utf-8', errors='replace')
            self.pending.clear()


def main(argv=None):
    # Leave the analyzer's normal options and defaults untouched.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--input-manifest', type=Path)
    opts, remaining = parser.parse_known_args(argv)
    if opts.input_manifest is None:
        return main_from_arguments(remaining)
    files = json.loads(opts.input_manifest.read_text(encoding='utf-8'))
    if not isinstance(files, list) or not files or not all(isinstance(p, str) for p in files):
        raise ValueError('Input manifest must be a nonempty JSON array of file paths')
    return main_from_arguments(remaining + files)


def main_from_arguments(arguments):
    script = Path(__file__).with_name('extract_mongodb_log.py')
    sys.path.insert(0, str(script.parent))
    try:
        original = runpy.run_path(str(script), run_name='mongodb_multifile_analyzer')
        old_argv = sys.argv
        try:
            sys.argv = [str(script)] + arguments
            args = original['parse_args']()
            sequences = input_sequences(args.inputs)
            ordered = [p for sequence in sequences for p in sequence]
            # Capture normal options from the existing parser without guessing its schema.
            namespace = original['main'].__globals__
            args.inputs = ordered
            namespace['parse_args'] = lambda: args
            namespace['read_lines'] = MultiFileLines(sequences)
            return original['main']()
        finally:
            sys.argv = old_argv
    finally:
        sys.path.pop(0)


if __name__ == '__main__':
    raise SystemExit(main())
