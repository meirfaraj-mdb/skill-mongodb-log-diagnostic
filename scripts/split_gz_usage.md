# Splitting a gzip file

## Installation

Place `split_gz.py` in the repository’s `scripts/` directory. Python 3 is required; no external packages are needed. Run commands from the repository root.

## Basic usage

```bash
python3 scripts/split_gz.py mongodb.log.gz
```

The script decompresses the input as a stream and splits it into parts of up to 1 GiB (1,073,741,824 uncompressed bytes). Every part except the final one is gzip-compressed. The final part remains uncompressed, even when it is exactly 1 GiB. The original input is preserved.

Output defaults to a directory beside the input named `mongodb.log.parts/`. Each output path is printed when processing succeeds.

Example output for a file requiring three parts:

```text
mongodb.log.parts/
  mongodb.log.part-00001.gz
  mongodb.log.part-00002.gz
  mongodb.log.part-00003
```

Compression uses gzip (`.gz`), not ZIP archives. Compressed parts can be smaller or slightly larger than their uncompressed size.

## Options

Choose a new output directory:

```bash
python3 scripts/split_gz.py mongodb.log.gz --output-dir ./output/mongodb-parts
```

Use decimal 1 GB instead of binary 1 GiB:

```bash
python3 scripts/split_gz.py mongodb.log.gz --part-size-bytes 1000000000
```

Use smaller parts for a quick test:

```bash
python3 scripts/split_gz.py mongodb.log.gz --part-size-bytes 10485760
```

Show help:

```bash
python3 scripts/split_gz.py --help
```

## Behavior and precautions

* The input must be an existing file with a `.gz` extension.
* The output directory must not already exist. Choose another path rather than deleting previous results inadvertently.
* Part size is measured in decompressed bytes, not compressed input bytes.
* Splits are byte-exact and may divide a log line or multibyte character. Individual parts are not guaranteed to be independently parseable logs.
* Inputs smaller than the part size produce one uncompressed part. Empty inputs produce one empty uncompressed part.
* Processing uses bounded-memory streaming, but needs disk space for accumulated output and the current uncompressed/compressed part.
* Results are published only after successful processing. On a decompression or compression failure, temporary output is removed.
* The script does not redact data. Parts inherit the input’s sensitivity; use an appropriately protected output location.

## Reconstructing the original decompressed file

For the example above, concatenate decompressed parts in numeric order, followed by the uncompressed last part:

```bash
{
  gzip -dc mongodb.log.parts/mongodb.log.part-00001.gz
  gzip -dc mongodb.log.parts/mongodb.log.part-00002.gz
  cat mongodb.log.parts/mongodb.log.part-00003
} > mongodb.reconstructed.log
```

To compare it with the original decompressed input using Bash:

```bash
cmp <(gzip -dc mongodb.log.gz) mongodb.reconstructed.log
```

No output and exit status 0 indicate identical bytes. Reconstruction requires additional disk space.
