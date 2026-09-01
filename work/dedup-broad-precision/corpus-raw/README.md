# Sanitized production dedup evidence

`dedup-pairs.json` is the minimal evidence set behind the broad-pair precision
regression test. It contains public article URLs, original titles, normalized
source names, existing dedup fingerprints, and operator labels only.

## Provenance

- The evidence snapshot covers soft flags recorded through 2026-08-19.
- The snapshot contained 31 historical flag pairs. Both pair fingerprints were
  still usable for 22 of them; nine had at least one deleted or legacy-format
  fingerprint without the `pairs` field.
- All 22 fully preserved pairs are included. Operator review classified the
  Car Culture pair as `dupe` and the other 21 as `not_a_dupe`.
- Two additional known duplicates from the incomplete group are included after
  reconstructing only their missing t-hunted side with the public production
  parser and `model_extractor.extract_fingerprint`: Team Transport and
  Boulevard. Their counterpart fingerprints were preserved in the snapshot.
- The remaining seven incomplete pairs are excluded because their historical
  pair-rule input cannot be reconstructed without inventing evidence.

This yields exactly 24 labelled pairs: three duplicates and 21 non-duplicates.

## Sanitization and derivation

The database was opened read-only (`mode=ro` plus `PRAGMA query_only=ON`). The
export selected only ledger URLs and the matching public title, source, and
fingerprint columns. Snapshot timestamps were used solely as a server-side
cutoff and were not exported.

Each article side has exactly four fields:

- `public_id`: canonical public HTTPS article URL;
- `title`: original public article title;
- `source_name`: normalized public source identifier;
- `fingerprint`: sorted `strict`, `brands`, `series`, and `pairs` lists.

The file contains no raw database rows, timestamps, environment data, private
operator data, credentials, chat identifiers, deployment coordinates, or query
parameters. The stable test fixture is derived from this file by assigning
sequential `prod-NNN` identifiers and adding the frozen baseline score.
