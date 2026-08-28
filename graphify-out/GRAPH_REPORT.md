# Graph Report - /home/eyeclept/Documents/Code/Projects/ODDArchiver  (2026-08-19)

## Corpus Check
- Corpus is ~21,476 words - fits in a single context window. You may not need a graph.

## Summary
- 331 nodes · 642 edges · 15 communities (12 shown, 3 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 11 edges (avg confidence: 0.53)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Disc burn backends|Disc burn backends]]
- [[_COMMUNITY_Crypto backends|Crypto backends]]
- [[_COMMUNITY_CLI dispatch|CLI dispatch]]
- [[_COMMUNITY_run.py helpers|run.py helpers]]
- [[_COMMUNITY_Delta compression|Delta compression]]
- [[_COMMUNITY_Restore and manifests|Restore and manifests]]
- [[_COMMUNITY_CLI argparse builders|CLI argparse builders]]
- [[_COMMUNITY_Local blob cache|Local blob cache]]
- [[_COMMUNITY_Docs command surface|Docs command surface]]
- [[_COMMUNITY_Logging and capacity|Logging and capacity]]
- [[_COMMUNITY_Config loading|Config loading]]
- [[_COMMUNITY_ISO vs disc backends|ISO vs disc backends]]
- [[_COMMUNITY_Package init|Package init]]
- [[_COMMUNITY_Package root|Package root]]
- [[_COMMUNITY_Tmp fixture file|Tmp fixture file]]

## God Nodes (most connected - your core abstractions)
1. `Manifest` - 20 edges
2. `_run_sync()` - 17 edges
3. `_run_dry_run()` - 15 edges
4. `CryptoBackend` - 15 edges
5. `ISOBackend` - 15 edges
6. `ManifestEntry` - 15 edges
7. `read_manifest()` - 15 edges
8. `_make_backend()` - 14 edges
9. `_run_init()` - 14 edges
10. `BurnBackend` - 14 edges

## Surprising Connections (you probably didn't know these)
- `ODDArchiver CLI` --reads--> `config.toml`  [EXTRACTED]
  README.md → SETUP.md
- `future PAR2 recovery` --extends--> `verify command`  [INFERRED]
  STATE_OF_THE_PROGRAM.md → README.md
- `CacheManager` --uses--> `BurnBackend`  [INFERRED]
  oddarchiver/cache.py → oddarchiver/disc.py
- `ManifestEntry` --uses--> `CryptoBackend`  [INFERRED]
  oddarchiver/manifest.py → oddarchiver/crypto.py
- `ManifestEntry` --uses--> `NullCrypto`  [INFERRED]
  oddarchiver/manifest.py → oddarchiver/crypto.py

## Import Cycles
- None detected.

## Communities (15 total, 3 thin omitted)

### Community 0 - "Disc burn backends"
Cohesion: 0.05
Nodes (39): process_files(), Input:  jobs        - list of (old_bytes, new_path) pairs             max_worker, BurnBackend, _copy_staging(), DiscBackend, DiscInfo, _find_mount(), ISOBackend (+31 more)

### Community 1 - "Crypto backends"
Cohesion: 0.06
Nodes (42): Any, CryptoBackend, generate_keyfile(), KeyfileCrypto, NullCrypto, PassphraseCrypto, Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     En, Input:  keyfile_path - path to a file that holds a 32-byte symmetric key     Out (+34 more)

### Community 2 - "CLI dispatch"
Cohesion: 0.08
Nodes (53): Namespace, CacheManager, Input:  cache_dir - root directory for the cache (default ~/.cache/oddarchiver), _backend_id(), _crypto_for_disc(), dispatch(), _encryption_block(), _fmt_bytes() (+45 more)

### Community 3 - "run.py helpers"
Cohesion: 0.08
Nodes (27): appendODD(), calculateSha256(), function(), gzIsDiff(), initODD(), listGzFromDir(), main(), makeFilename() (+19 more)

### Community 4 - "Delta compression"
Cohesion: 0.12
Nodes (22): apply_delta(), compute_delta(), delta_or_full(), _memfd_with_bytes(), Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     xd, Create an anonymous in-memory file, write data, and seek back to 0.      Returns, Input:  old_bytes - plaintext content of the previous version             new_pa, Input:  base_bytes  - plaintext content of the base version             delta_by (+14 more)

### Community 5 - "Restore and manifests"
Cohesion: 0.14
Nodes (23): ManifestEntry, Return path if it is a legal blob path. Raise ValueError otherwise., Join root / rel_path and keep the result under root.      Rejects absolute paths, Single file entry within a session manifest., safe_join_under(), validate_blob_path(), _apply_delta_entry(), _build_chains() (+15 more)

### Community 6 - "CLI argparse builders"
Cohesion: 0.16
Nodes (18): ArgumentParser, _add_dry_iso_mutex(), _add_history(), _add_init(), _add_restore(), _add_status(), _add_sync(), _add_verify() (+10 more)

### Community 7 - "Local blob cache"
Cohesion: 0.17
Nodes (12): _blob_path(), _load_manifest(), _manifest_path(), Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     Lo, Input:  path    - relative file path                 session - session number, Input:  path    - relative file path                 session - session number, Input:  cache_dir - cache root             path      - relative file path, Input:  cache_dir - cache root     Output: Path to the cache manifest JSON file (+4 more)

### Community 8 - "Docs command surface"
Cohesion: 0.13
Nodes (15): history command, init command, restore command, status command, sync command, verify command, config.toml, pip extra encrypt (+7 more)

### Community 9 - "Logging and capacity"
Cohesion: 0.18
Nodes (14): Logger, check_capacity(), _fmt_bytes(), main(), Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     St, Input:  logger - Logger instance to emit on             msg    - log message (po, Input:  used_pct        - percentage of disc capacity used (0-100)             r, Input:  None     Output: None     Details:         Smoke-test: emit one message (+6 more)

### Community 10 - "Config loading"
Cohesion: 0.24
Nodes (11): Config, load_config(), main(), Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     Co, Input:  args - parsed argparse.Namespace. Attributes that are not None override, Input:  None     Output: None     Details:         Prints values from load_confi, Input:  N/A - constructed by load_config or directly     Output: N/A     Details, Input:  None     Output: tomllib module or None if unavailable     Details: (+3 more)

### Community 11 - "ISO vs disc backends"
Cohesion: 0.67
Nodes (4): BurnBackend interface, DiscBackend physical BD-R, ISOBackend test mode, 155 pytest tests

## Knowledge Gaps
- **12 isolated node(s):** `oddarchiver`, `restore command`, `history command`, `status command`, `xdelta3 binary deltas` (+7 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ISOBackend` connect `Disc burn backends` to `CLI dispatch`?**
  _High betweenness centrality (0.060) - this node is a cross-community bridge._
- **Why does `BurnBackend` connect `Disc burn backends` to `Crypto backends`, `CLI dispatch`, `Delta compression`, `Restore and manifests`, `Local blob cache`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._
- **Why does `DiscBackend` connect `Disc burn backends` to `CLI dispatch`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `Manifest` (e.g. with `CryptoBackend` and `KeyfileCrypto`) actually correct?**
  _`Manifest` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `CryptoBackend` (e.g. with `Manifest` and `ManifestEntry`) actually correct?**
  _`CryptoBackend` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     OD`, `Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     En`, `Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     Lo` to the rest of the system?**
  _159 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Disc burn backends` be split into smaller, more focused modules?**
  _Cohesion score 0.0519774011299435 - nodes in this community are weakly interconnected._