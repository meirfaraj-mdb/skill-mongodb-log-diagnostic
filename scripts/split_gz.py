#!/usr/bin/env python3
"""Stream a .gz into byte-sized parts; gzip all except the final part.

Default part size: 1 GiB (1,073,741,824 decompressed bytes).
Input is preserved. Output directory must not exist. Byte splits may cut lines.
Usage: python3 scripts/split_gz.py mongodb.log.gz --output-dir mongodb.parts
"""
import argparse
import gzip
import shutil
import tempfile
from pathlib import Path

BLOCK_SIZE = 1024 * 1024


def split_gz(source, output_dir=None, part_size=1024 ** 3):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError(f"Input is not a file: {source}")
    if source.suffix.lower() != '.gz':
        raise ValueError('Input must have a .gz extension')
    if part_size <= 0:
        raise ValueError('Part size must be positive')
    stem = source.name[:-3]
    destination = Path(output_dir).resolve() if output_dir else source.parent / (stem + '.parts')
    if destination.exists():
        raise FileExistsError(f"Output directory already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.split-gz-', dir=destination.parent))
    names = []
    try:
        with gzip.open(source, 'rb') as stream:
            pending = stream.read(1)
            number = 1
            # An empty input produces one empty, uncompressed final part.
            while True:
                part = temporary / f'{stem}.part-{number:05d}'
                with part.open('wb') as target:
                    target.write(pending)
                    remaining = part_size - len(pending)
                    while remaining:
                        chunk = stream.read(min(BLOCK_SIZE, remaining))
                        if not chunk:
                            break
                        target.write(chunk)
                        remaining -= len(chunk)
                pending = stream.read(1)  # Also consumes the gzip trailer at EOF.
                if pending:
                    compressed = part.with_name(part.name + '.gz')
                    with part.open('rb') as incoming, gzip.open(compressed, 'wb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing, length=BLOCK_SIZE)
                    part.unlink()
                    names.append(compressed.name)
                    number += 1
                else:
                    names.append(part.name)
                    break
        # Publish only after successful decompression and compression.
        if destination.exists():
            raise FileExistsError(f"Output directory already exists: {destination}")
        temporary.rename(destination)
        return [destination / name for name in names]
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('input', type=Path, help='Source gzip file (preserved)')
    parser.add_argument('--output-dir', type=Path, help='New output directory; default: <input without .gz>.parts')
    parser.add_argument('--part-size-bytes', type=int, default=1024 ** 3, help='Decompressed bytes per part; default: 1073741824 (1 GiB)')
    args = parser.parse_args()
    try:
        files = split_gz(args.input, args.output_dir, args.part_size_bytes)
    except (OSError, EOFError, ValueError) as exc:
        parser.exit(1, f'Error: {exc}\n')
    for file in files:
        print(file)


if __name__ == '__main__':
    main()
