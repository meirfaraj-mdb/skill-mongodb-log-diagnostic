# Direct multi-file extraction

Copy the ZIP files to their matching repository paths. Replace agent/extract_stage.py; add scripts/extract_mongodb_logs.py inside skills/mongodb-log-diagnostic. Keep the original extract_mongodb_log.py and SKILL.md unchanged. This adapter preserves the original analyzer and its options.

## Skill workflow

For one node and analysis period, pass every supplied MongoDB log to one invocation. Do not analyze raw logs in the assistant, run one extraction per file, or concatenate/recompress the inputs beforehand. Do not combine different nodes. Use only the structured extraction output for analysis.

```bash
python3 skills/mongodb-log-diagnostic/scripts/extract_mongodb_logs.py \
  --output /tmp/node-extract --slow-ms 1000 \
  /path/mongodb.log.part-00001.gz /path/mongodb.log.part-00002
```

Alternatively, --input-manifest accepts a JSON array of absolute local input paths. The stage uses this to avoid argument-length limits. The adapter delegates other options, including FTDC, to the original parser. Files remain on disk, while records are streamed into one shared accumulator.

## Behavior

Numbered .part-N[.gz] sequences are naturally ordered and joined at the byte-stream boundary before UTF-8 decoding. Independent files are never joined into one record. Missing/duplicate part numbers, duplicate paths, and supplying both an original and its split parts fail explicitly. A cross-part record is attributed to the part where the record ends. Overlapping records from independently rotated logs are not deduplicated.

The stage downloads original files without decompressing/recompressing them. All original inputs for one node must fit on local disk (on memory-backed storage this also consumes memory). Set MONGODB_LOG_DIAG_EXTRACT_DOWNLOAD_WORKERS=4 only if the configured store supports concurrent downloads safely; default 1, maximum 16. This changes download concurrency, not parser concurrency. It removes the extra gzip pass but no production speedup has been measured.

Outputs retain extracts/mongodb/. The source manifest version changes to 2, causing the first run to rebuild prior extracts. The manifest records keys, not content versions: force re-extraction if contents change under the same keys. Existing input_bucket routing remains in the provider; this change does not modify providers.py.

## Validation

```bash
python3 -m unittest discover -s tests -p test_direct_multifile_extraction.py -v
python3 -m compileall -q agent skills/mongodb-log-diagnostic/scripts
```

Eight focused tests cover the adapter with a synthetic analyzer and stage storage mocks. Full original-parser integration, cloud deployment, and production performance are not verified. Review the stage diff against your checkout because its base is the previous node-grouped replacement, not a formatting-preserving current repository copy.
