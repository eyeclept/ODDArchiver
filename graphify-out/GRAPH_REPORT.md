# Graph Report - .  (2026-10-20)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 1553 nodes · 3859 edges · 70 communities (66 shown, 4 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 209 edges (avg confidence: 0.57)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `00bf34ea`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Cryptographic Ciphers and KDF|Cryptographic Ciphers and KDF]]
- [[_COMMUNITY_Session Verification Logic|Session Verification Logic]]
- [[_COMMUNITY_Restore Path Versioning|Restore Path Versioning]]
- [[_COMMUNITY_File Encryption and Sessions|File Encryption and Sessions]]
- [[_COMMUNITY_Manifest and File Hashing|Manifest and File Hashing]]
- [[_COMMUNITY_Blob Cache Management|Blob Cache Management]]
- [[_COMMUNITY_Architecture and Design Docs|Architecture and Design Docs]]
- [[_COMMUNITY_Disc Encryption Mode Handling|Disc Encryption Mode Handling]]
- [[_COMMUNITY_Software Versioning and Bundling|Software Versioning and Bundling]]
- [[_COMMUNITY_Catalog Persistence and Merging|Catalog Persistence and Merging]]
- [[_COMMUNITY_Archive Restore Logic|Archive Restore Logic]]
- [[_COMMUNITY_Main CLI Dispatcher|Main CLI Dispatcher]]
- [[_COMMUNITY_CLI Argument Parsing|CLI Argument Parsing]]
- [[_COMMUNITY_Disc Burning and Media|Disc Burning and Media]]
- [[_COMMUNITY_Catalog Browsing and Navigation|Catalog Browsing and Navigation]]
- [[_COMMUNITY_Volume Preparation and Reading|Volume Preparation and Reading]]
- [[_COMMUNITY_Dry Run File Processing|Dry Run File Processing]]
- [[_COMMUNITY_Disc File Extraction|Disc File Extraction]]
- [[_COMMUNITY_CLI Burn Operations|CLI Burn Operations]]
- [[_COMMUNITY_Blob Restore Chain|Blob Restore Chain]]
- [[_COMMUNITY_Session Delta Processing|Session Delta Processing]]
- [[_COMMUNITY_Logging and Status Reporting|Logging and Status Reporting]]
- [[_COMMUNITY_Session Manifest Management|Session Manifest Management]]
- [[_COMMUNITY_Full Image Extraction|Full Image Extraction]]
- [[_COMMUNITY_Checksum and Failure Reporting|Checksum and Failure Reporting]]
- [[_COMMUNITY_Recovery and Safety Docs|Recovery and Safety Docs]]
- [[_COMMUNITY_Encryption Backend Interface|Encryption Backend Interface]]
- [[_COMMUNITY_Burn Backend Interface|Burn Backend Interface]]
- [[_COMMUNITY_Disc Acquisition and Results|Disc Acquisition and Results]]
- [[_COMMUNITY_ISO Backend Management|ISO Backend Management]]
- [[_COMMUNITY_Volume Manifest and Baseline|Volume Manifest and Baseline]]
- [[_COMMUNITY_Project Overview and Features|Project Overview and Features]]
- [[_COMMUNITY_Volume Initialization and Sync|Volume Initialization and Sync]]
- [[_COMMUNITY_Configuration Management|Configuration Management]]
- [[_COMMUNITY_ISO Path Reading|ISO Path Reading]]
- [[_COMMUNITY_Binary Delta Computation|Binary Delta Computation]]
- [[_COMMUNITY_Burn Error Handling|Burn Error Handling]]
- [[_COMMUNITY_Baseline History Decoding|Baseline History Decoding]]
- [[_COMMUNITY_Staging and Staking Docs|Staging and Staking Docs]]
- [[_COMMUNITY_Ledger and Session Coverage|Ledger and Session Coverage]]
- [[_COMMUNITY_CLI Command Overview|CLI Command Overview]]
- [[_COMMUNITY_Sync Group Staging|Sync Group Staging]]
- [[_COMMUNITY_Staging Workspace Management|Staging Workspace Management]]
- [[_COMMUNITY_Control Files and NullCrypto|Control Files and NullCrypto]]
- [[_COMMUNITY_Installation and Setup|Installation and Setup]]
- [[_COMMUNITY_Catalog Event Indexing|Catalog Event Indexing]]
- [[_COMMUNITY_Disc Crypto Initialization|Disc Crypto Initialization]]
- [[_COMMUNITY_Physical Disc Backend|Physical Disc Backend]]
- [[_COMMUNITY_Multi-volume Restore Logic|Multi-volume Restore Logic]]
- [[_COMMUNITY_Multi-volume Archive Docs|Multi-volume Archive Docs]]
- [[_COMMUNITY_External Tool Integration|External Tool Integration]]
- [[_COMMUNITY_Archive Catalog Building|Archive Catalog Building]]
- [[_COMMUNITY_Sequential Session Reading|Sequential Session Reading]]
- [[_COMMUNITY_Trailing Session Detection|Trailing Session Detection]]
- [[_COMMUNITY_Encryption and Rescue Keys|Encryption and Rescue Keys]]
- [[_COMMUNITY_Verification and Status Docs|Verification and Status Docs]]
- [[_COMMUNITY_Project Development Status|Project Development Status]]
- [[_COMMUNITY_Legacy Format Compatibility|Legacy Format Compatibility]]
- [[_COMMUNITY_Backend Identity and Verification|Backend Identity and Verification]]
- [[_COMMUNITY_Blob Capture and Checking|Blob Capture and Checking]]
- [[_COMMUNITY_Suspect Manifest Tracking|Suspect Manifest Tracking]]
- [[_COMMUNITY_Package Metadata|Package Metadata]]
- [[_COMMUNITY_Browsing and Restore Docs|Browsing and Restore Docs]]
- [[_COMMUNITY_Package Name|Package Name]]
- [[_COMMUNITY_Test Dependencies|Test Dependencies]]
- [[_COMMUNITY_Test Fixtures|Test Fixtures]]
- [[_COMMUNITY_Media and Hardware Docs|Media and Hardware Docs]]
- [[_COMMUNITY_Catalog Candidate Resolution|Catalog Candidate Resolution]]
- [[_COMMUNITY_Command Reference|Command Reference]]
- [[_COMMUNITY_Disc Authentication and Loading|Disc Authentication and Loading]]

## God Nodes (most connected - your core abstractions)
1. `Manifest` - 96 edges
2. `CryptoBackend` - 77 edges
3. `BurnBackend` - 77 edges
4. `ManifestEntry` - 59 edges
5. `ArchiveCatalog` - 55 edges
6. `PathVersion` - 55 edges
7. `VolumeMarker` - 52 edges
8. `DiscBackend` - 38 edges
9. `_PreparedBlob` - 37 edges
10. `CacheManager` - 31 edges

## Surprising Connections (you probably didn't know these)
- `future PAR2 recovery` --extends--> `verify command`  [INFERRED]
  STATE_OF_THE_PROGRAM.md → README.md
- `CacheManager` --uses--> `CryptoBackend`  [INFERRED]
  oddarchiver/cache.py → oddarchiver/crypto.py
- `CacheManager` --uses--> `BurnBackend`  [INFERRED]
  oddarchiver/cache.py → oddarchiver/disc.py
- `CacheManager` --uses--> `Manifest`  [INFERRED]
  oddarchiver/cache.py → oddarchiver/manifest.py
- `_ControlFiles` --uses--> `CacheManager`  [INFERRED]
  oddarchiver/cli.py → oddarchiver/cache.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Encryption Pipeline** — oddarchiver_crypto, argon2id, chacha20_poly1305, oddarchiver_session [EXTRACTED 1.00]
- **Burn Pipeline** — oddarchiver_session, oddarchiver_disc, oddarchiver_manifest, growisofs [EXTRACTED 1.00]
- **Restore Flow** — oddarchiver_restore, oddarchiver_manifest, oddarchiver_delta, oddarchiver_crypto [EXTRACTED 1.00]

## Communities (70 total, 4 thin omitted)

### Community 0 - "Cryptographic Ciphers and KDF"
Cohesion: 0.06
Nodes (46): Argon2id KDF, ChaCha20-Poly1305 Cipher, ChaCha20Poly1305, Input:  mode - disc encryption mode; enc_mode_data - parsed mode metadata     Ou, _rescue_disc_crypto(), _copy_null_crypto_range(), _decrypt_bound_chunks(), _decrypt_chunks() (+38 more)

### Community 1 - "Session Verification Logic"
Cohesion: 0.18
Nodes (13): _check_fast(), _print_session_incomplete(), _print_session_ok(), Input:  s - session index; incomplete_idx - incomplete slot, if any;, Input:  backend - disc backend; crypto - decryption backend; level - check depth, Input:  backend   - BurnBackend to read manifests and blobs from             cry, Input:  manifest      - Manifest to check             session_idx   - expected s, Print a single OK session line matching DesignDoc format. (+5 more)

### Community 2 - "Restore Path Versioning"
Cohesion: 0.08
Nodes (45): _acquired_marker(), _add_discovery_discs(), _base_candidate(), _chain(), _chain_base_possible(), _chain_complete(), _chain_discs_possible(), _chain_hints_possible() (+37 more)

### Community 3 - "File Encryption and Sessions"
Cohesion: 0.07
Nodes (30): Input:  src    - plaintext file                 dest   - ciphertext destination, Input:  src  - ciphertext file produced by encrypt_file()                 dest -, Input:  staging                - path to prepared session_000 directory tree, Input:  staging                - path to prepared session_NNN directory tree, Input:  session_n - session index to make available locally         Output: loca, Join root / rel_path and keep the result under root.      Rejects absolute paths, safe_join_under(), _dest_digest() (+22 more)

### Community 4 - "Manifest and File Hashing"
Cohesion: 0.05
Nodes (98): blob_aad(), Input: a manifest entry. Output: authenticated physical blob identity., Shared interface that the session-ordered replay engines of restore and verify r, SessionSource, hash_file(), Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     Ch, Input:  path - file to hash             chunk_size - number of bytes read per ch, ManifestEntry (+90 more)

### Community 5 - "Blob Cache Management"
Cohesion: 0.08
Nodes (41): _blob_path(), _blob_rel_for_path(), CacheManager, default_cache_dir(), _key_path(), _legacy_cache_entry(), _load_manifest(), _load_session_manifest_from_disc() (+33 more)

### Community 6 - "Architecture and Design Docs"
Cohesion: 0.04
Nodes (45): 100GB BDXL (triple layer), 25GB BD-R (single layer), Architecture, CLI Interface, Configuration, Configuration, Constraints, Cron Integration (+37 more)

### Community 7 - "Disc Encryption Mode Handling"
Cohesion: 0.08
Nodes (32): Input:  archive_id - ID from volume.json     Output: none, passphrase, or keyfil, Input:  archive_id - ID from volume.json             mode - none, passphrase, or, remember_encryption_mode(), remembered_encryption_mode(), _check_remembered_disc_mode(), _confirm_plaintext_disc(), _crypto_and_manifests(), _crypto_for_disc() (+24 more)

### Community 8 - "Software Versioning and Bundling"
Cohesion: 0.08
Nodes (37): _copy_filtered_tree_entry(), _copy_minimal_package_file(), _copy_repository_file(), _copy_tree_filtered(), current_version(), _dir_bytes(), package_dir(), parse_version_marker() (+29 more)

### Community 9 - "Catalog Persistence and Merging"
Cohesion: 0.12
Nodes (31): archive_memory_path(), _catalog_tree_version(), CatalogDisc, _memory_path(), _merge_disc(), _merge_remembered_discs(), _merge_saved_catalog_discs(), _parse_memory_record() (+23 more)

### Community 10 - "Archive Restore Logic"
Cohesion: 0.10
Nodes (35): ledger_dir(), Input:  None     Output: Path - directory that holds <label>.ledger.json files, _initial_missing_prompt(), Input: destination, state, missing discs and index. Output: persisted missing di, Input: inserted disc, archive catalog, selection and acquisition policy, Input: restore destination, state, catalog, selection and disc availability, Input: destination, archive identity, requested and effective selection     Outp, _record_missing_disc() (+27 more)

### Community 11 - "Main CLI Dispatcher"
Cohesion: 0.07
Nodes (34): Namespace, highest_seen_volume(), Input:  archive_id - archive ID from volume.json     Output: Highest locally see, _apply_skip_large(), _browse_restore(), dispatch(), _dry_run_changes(), _listing_catalog() (+26 more)

### Community 12 - "CLI Argument Parsing"
Cohesion: 0.11
Nodes (46): ArgumentParser, _add_browse(), _add_device(), _add_disc_size(), _add_dry_iso_mutex(), _add_dry_run(), _add_embed_software(), _add_history() (+38 more)

### Community 13 - "Disc Burning and Media"
Cohesion: 0.07
Nodes (37): Match, _copy_file_synced(), _copy_staging(), _display_burn_progress(), _fsync_dir(), _is_no_media_text(), _log_burn_signal(), _parse_mediainfo() (+29 more)

### Community 14 - "Catalog Browsing and Navigation"
Cohesion: 0.07
Nodes (44): catalog_tree(), disc_name(), Input:  catalog - path history             at_session - last session to include,, Input:  catalog - known disc records             volume_index - disc index, if k, Input: text - untrusted disc label or archive path     Output: Text with termina, safe_display(), _browse_navigation(), _browse_number() (+36 more)

### Community 15 - "Volume Preparation and Reading"
Cohesion: 0.27
Nodes (10): _prepare_next_volume(), Input:  backend and crypto - current volume and baseline reader             labe, Input:  args - init namespace; session_mod - staging module; group_index and, Input:  backend - active backend after a group burn             crypto  - archiv, Input:  backend - BurnBackend to read from             crypto  - CryptoBackend f, _read_disc_manifests(), _refresh_sync_volume(), _stage_init_group() (+2 more)

### Community 16 - "Dry Run File Processing"
Cohesion: 0.17
Nodes (12): _dry_new_files(), _print_dry_changed(), _print_dry_space(), Input:  source - directory to scan for dry-run files     Output: source-relative, Input:  changed, source, delta_results - changed paths and their computed deltas, Input:  total_session_bytes - estimated bytes in this session             remain, Input:  new_files, source - new source-relative paths and their root     Output:, Input:  args    - parsed namespace with source and backend flags             is_ (+4 more)

### Community 17 - "Disc File Extraction"
Cohesion: 0.09
Nodes (17): FileNotFoundError, Input:  path        - relative path on the disc filesystem                 dest, Input:  path - relative path within the disc filesystem         Output: bytes, Input:  path - relative path within the disc/ISO filesystem         Output: byte, Input:  path - relative path within the disc/ISO filesystem                 dest, Input:  s - prior session number         Output: bool - True if manifest.enc or, Input:  attempted_session - session number that just failed to burn         Outp, Input:  path - relative path on the disc filesystem                 dest - desti (+9 more)

### Community 18 - "CLI Burn Operations"
Cohesion: 0.08
Nodes (38): _backend_id(), _burn_mirror(), _burn_primary(), _burn_sync_group(), _burn_verify_commit(), _burn_volume_chunks(), _cache_payloads(), _chunk_burn_options() (+30 more)

### Community 19 - "Blob Restore Chain"
Cohesion: 0.13
Nodes (20): _blob(), _collected_chain_entries(), _entry_key(), _needed_for_pending(), Input: catalog, pending chains, missing and learned discs, state and spool     O, Input: manifests and validated requests. Output: copies containing only requeste, Input: session source, manifests and storage paths. Output: validated stored blo, Input: requested entries, blob roots and restore state. Output: verified recorde (+12 more)

### Community 20 - "Session Delta Processing"
Cohesion: 0.04
Nodes (69): apply_delta(), process_bytes_files(), Input: jobs - previous and new content pairs     Output: One ("delta", bytes) or, Input:  base_bytes  - plaintext content of the base version             delta_by, _attach_dedup_refs(), _blob_too_large_error(), _build_checksum_index(), _classify_staging_paths() (+61 more)

### Community 21 - "Logging and Status Reporting"
Cohesion: 0.11
Nodes (22): Logger, LogRecord, _print_status_suspects(), Input:  loaded session manifests     Output: printed suspect rows, check_capacity(), _ConsoleFormatter, _create_log_parents(), main() (+14 more)

### Community 22 - "Session Manifest Management"
Cohesion: 0.07
Nodes (41): Any, _patch_manifest(), Input:  staging    - staging root directory             session_n  - session ind, is_bound_ciphertext(), manifest_aad(), Input: ciphertext bytes. Output: True for a format 3 envelope., Input: session slot. Output: manifest AAD., archive_session_count() (+33 more)

### Community 23 - "Full Image Extraction"
Cohesion: 0.18
Nodes (8): FullImageSource, make_session_source(), _materialize(), Input:  backend - BurnBackend to batch-read from             paths   - disc-rela, --read-mode full-copy: every session extracted up front into one local directory, Input:  backend, manifests - archive to extract                 workdir, Input:  session_n - session to make locally available         Output: workdir, n, Input:  backend, manifests - archive to read from             workdir

### Community 24 - "Checksum and Failure Reporting"
Cohesion: 0.22
Nodes (8): _Hash, _append_checksum_mismatch(), _print_session_fail(), Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     In, Input: split entry state. Output: digest, next index, and target when valid., Input: checksum mismatch details. Output: None (appends to errors)., Print a FAIL session block with per-error detail., _split_digest_state()

### Community 25 - "Recovery and Safety Docs"
Cohesion: 0.12
Nodes (15): all mode, Embedded restore software, How status and verify surface it, How to find the software directory, How to restore from that directory, minimal mode, Partial and failed write recovery, Recover older delta chains (+7 more)

### Community 26 - "Encryption Backend Interface"
Cohesion: 0.08
Nodes (48): CryptoBackend, new_blob_aad(), Input:  ciphertext - bytes produced by encrypt()         Output: plaintext bytes, Input: the original session, kind, and blob name. Output: blob AAD., Abstract interface for encrypt/decrypt operations., Input:  plaintext - raw bytes to encrypt         Output: ciphertext bytes (nonce, DiscInfo, Snapshot of disc/ISO capacity state. (+40 more)

### Community 27 - "Burn Backend Interface"
Cohesion: 0.09
Nodes (25): ArchiveCatalog, Store path events from earlier discs and the inserted disc., BurnBackend, Abstract interface for disc-write and disc-read operations., Input:  None         Output: DiscInfo - current capacity snapshot         Detail, Input:  None         Output: session numbers of the session_NNN root directories, Input:  None         Output: None         Details:             Clears any cached, _history_before_missing_gap() (+17 more)

### Community 28 - "Disc Acquisition and Results"
Cohesion: 0.14
Nodes (8): DiscAcquisition, Get a needed disc from a drive or sibling ISO. Tests can replace this object., Input: first backend, ISO path, input function. Output: disc source., Input: disc index. Output: conventional sibling ISO path for that index., Input: sibling ISO path and archive identity         Output: None         Detail, Input: archive identity. Output: None. Details: Read all sibling markers one tim, Return restored and failed file counts and recovery details., RestoreResult

### Community 29 - "ISO Backend Management"
Cohesion: 0.10
Nodes (19): check_volume_identity(), ISOBackend, Input:  expected               - archive session count recorded at, Input:  iso_path  - filesystem path for the ISO file             disc_size - sim, Input:  staging, label, expected_session_count,                 expected_archive, Input:  staging, label, expected_session_count,                 expected_archive, Input:  prefill_bytes - bytes to simulate as already used         Output: None, Input:  None         Output: DiscInfo         Details:             Counts sessio (+11 more)

### Community 30 - "Volume Manifest and Baseline"
Cohesion: 0.09
Nodes (47): Input:  backend, crypto, manifests - source disc and its manifests             m, _read_catalog_baseline(), Input:  staging and session_n - chunk location and archive session             i, _write_chunk_marker(), baseline_aad(), Input: volume identity. Output: baseline AAD., Manifest, Full session manifest. (+39 more)

### Community 31 - "Project Overview and Features"
Cohesion: 0.15
Nodes (13): Architecture, Dry run and test ISO mode, Encryption, Idempotency, Install, Key properties, More, Multi-drive redundancy (+5 more)

### Community 32 - "Volume Initialization and Sync"
Cohesion: 0.10
Nodes (23): _advance_init_volume(), _announce_init_groups(), _diff_sync_source(), _encryption_block(), _init_group_burn_options(), _make_mirror_backend(), Input:  crypto - CryptoBackend instance     Output: dict - encryption metadata t, Input:  source - source directory to scan     Output: mapping of source-relative (+15 more)

### Community 33 - "Configuration Management"
Cohesion: 0.20
Nodes (15): _apply_config_options(), _apply_config_paths(), Config, default_config_path(), load_config(), main(), Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     Th, Input:  path - the path to config.toml. If path is None, the function uses defau (+7 more)

### Community 34 - "ISO Path Reading"
Cohesion: 0.10
Nodes (16): _control_file_cap(), _copy_iso_blob(), Input:  src  - source blob in the ISO sessions tree             dest - destinati, Input:  paths - relative paths within the disc filesystem         Output: dict m, Input:  path - relative path within the disc filesystem                 dest - d, Input:  paths     - relative paths within the disc filesystem                 de, Input:  paths - relative paths within the disc/ISO filesystem         Output: di, Input:  paths     - relative paths within the disc/ISO filesystem (+8 more)

### Community 35 - "Binary Delta Computation"
Cohesion: 0.17
Nodes (14): compute_delta(), compute_delta_bytes(), delta_or_full(), _delta_or_full_bytes(), _memfd_with_bytes(), _ordered_delta_results(), Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     Th, Input:  old_bytes - previous plaintext version             new_path  - path to n (+6 more)

### Community 36 - "Burn Error Handling"
Cohesion: 0.12
Nodes (18): BurnInterrupted, _is_disc_full_signature(), _is_disconnect(), _is_hardware_fault(), _raise_disconnected(), Input:  output_lines - captured growisofs stdout/stderr lines     Output: bool -, Input:  output_lines - captured growisofs stdout/stderr lines     Output: bool -, Input:  staging - directory that should contain session_NNN/     Output: int ses (+10 more)

### Community 37 - "Baseline History Decoding"
Cohesion: 0.18
Nodes (15): _baseline_header_version(), _decode_baseline_history(), _decode_baseline_payload(), _nonnegative(), Check an integer field before using it as an index or session., Input:  raw - decoded baseline mapping; version - baseline format version     Ou, Input:  raw - decoded baseline; name - disc filename             marker - disc i, Input:  data - baseline payload; name - disc filename             marker - disc (+7 more)

### Community 38 - "Staging and Staking Docs"
Cohesion: 0.17
Nodes (12): Automatic staging-space chunking, Delta cache content, Encoding pre-pass, Lockfile, Packing budgets, Return contract, SIGINT, Source changes during staging (+4 more)

### Community 39 - "Ledger and Session Coverage"
Cohesion: 0.22
Nodes (10): _check_init_ledger(), Input:  args - parsed init namespace; backend - target disc; init_label - disc l, check_session_coverage(), ledger_path(), load_ledger(), Input:  label, backend_id - archive label and physical medium identity     Outpu, Input:  label              - archive volume label             backend_id, Input:  manifests          - session manifests already read from the disc (+2 more)

### Community 40 - "CLI Command Overview"
Cohesion: 0.17
Nodes (12): history command, init command, restore command, status command, sync command, verify command, Encryption modes none passphrase keyfile, future PAR2 recovery (+4 more)

### Community 41 - "Sync Group Staging"
Cohesion: 0.20
Nodes (10): _dry_prior_jobs(), _mask_current_paths(), _mask_diff_to_group(), _prior_blob_cache(), Input:  cache    - the local cache             no_cache - True when the user gav, Input:  changed, source, disc_origin, session_n - changed paths and prior sessio, Input:  current_state - current file checksums             disc_state - checksum, Input:  current_state - {path: sha256} for every file in source right now (+2 more)

### Community 42 - "Staging Workspace Management"
Cohesion: 0.06
Nodes (38): build_staging(), _check_staging_lock(), _check_staging_space(), _disc_changes(), _ensure_staging_dir(), _open_staging_workspace(), _prefetch_origin_blobs(), _print_bar() (+30 more)

### Community 43 - "Control Files and NullCrypto"
Cohesion: 0.22
Nodes (7): dict, _ControlFiles, Control-file bytes from one read_paths batch, plus the parsed marker., NullCrypto, Input:  None     Output: N/A (class)     Details:         Identity crypto. No en, BaselineReadError, Report an unreadable baseline without treating it as absent.

### Community 44 - "Installation and Setup"
Cohesion: 0.18
Nodes (11): Configuration file, Cron deployment, Development setup, Directories ODDArchiver uses, Enable unattended recovery for a stuck USB optical drive (optional, recommended for cron), Install the Python package, Keyfile mode, Passphrase mode (+3 more)

### Community 45 - "Catalog Event Indexing"
Cohesion: 0.12
Nodes (18): _apply_disc_events(), _disc_manifest_events(), _index_catalog(), learn_disc(), Input:  catalog - path history and disc ranges to index     Output: Resolved ver, Input:  catalog - known disc ranges             session - session number to loca, Input:  marker, manifests - disc run and loaded manifests     Output: Usable man, Input:  usable, marker - validated disc manifests and identity     Output: Path (+10 more)

### Community 46 - "Disc Crypto Initialization"
Cohesion: 0.12
Nodes (18): _attach_rescue_code(), _dry_run_disc_state(), _init_passphrase(), _make_init_crypto(), _ordinary_disc_crypto(), Input:  None     Output: passphrase bytes from the environment or confirmed prom, Input:  crypto - encrypted backend for an init session     Output: crypto with i, Input:  args - parsed init namespace with --encrypt and optional --key     Outpu (+10 more)

### Community 47 - "Physical Disc Backend"
Cohesion: 0.11
Nodes (15): DiscBackend, _publish_extracted_paths(), _pvd_label(), Input:  unique_paths - requested paths in extraction order                 max_b, Input:  paths     - relative paths on the disc filesystem                 dest_r, Input: paths and scratch root         Output: whether extraction is complete and, Input: disc paths, scratch root, retry policy         Output: None         Detai, Input:  descriptor - 2048-byte ISO9660 primary volume descriptor     Output: str (+7 more)

### Community 48 - "Multi-volume Restore Logic"
Cohesion: 0.20
Nodes (10): _build_chains(), _multivolume_chains(), Input:  manifests - list of Manifest objects in ascending session order     Outp, Input: selected manifests and the first session number on the disc     Output: n, Input: disc manifests, target session, path selection and recovery mode     Outp, Input:  dest, source, crypto, manifests, session, force, paths, skip_volumes, Input: inserted-disc manifests and prior restore state     Output: file chains e, _restore_legacy() (+2 more)

### Community 49 - "Multi-volume Archive Docs"
Cohesion: 0.20
Nodes (10): Archive, volume, and session numbers, Baseline on later discs, Disc-swap during init and sync, Multi-volume archives, Multi-volume restore, Older discs, On-disc format 2, Recovery mode (+2 more)

### Community 50 - "External Tool Integration"
Cohesion: 0.29
Nodes (7): BurnBackend Interface, DiscBackend physical BD-R, genisoimage, growisofs, ISOBackend test mode, 155 pytest tests, xorriso

### Community 51 - "Archive Catalog Building"
Cohesion: 0.25
Nodes (8): build_archive_catalog(), _catalog_complete(), _catalog_disc_records(), _catalog_last_session(), Input:  replay, baseline - collected volume histories             marker - sourc, Input:  manifests, history - source and replayed path events     Output: Last so, Input:  catalog, marker, baseline - resolved disc history and source     Output:, Input:  backend, crypto - source disc and its decryption backend             man

### Community 52 - "Sequential Session Reading"
Cohesion: 0.24
Nodes (8): Author: Richard Baldwin Date:   2026 Email:  eyeclept@pm.me  Description:     Se, --read-mode sequential (default): blobs of one session on local disk at a time., Input:  backend, manifests - archive to extract from, on demand, Input:  m - manifest whose entries reference blob paths     Output: valid blob p, Input:  manifests - Manifest list (any order)     Output: {session_n: [blob path, SequentialSource, _session_blob_paths(), _valid_session_blobs()

### Community 53 - "Trailing Session Detection"
Cohesion: 0.20
Nodes (10): _control_has_slot(), detect_incomplete_session(), _last_clean_and_unreadable(), _listed_trailing_sessions(), _probe_trailing_slot(), Input:  backend     - BurnBackend to inspect             manifests   - session i, Input: parsed manifests. Output: last clean session and trailing unreadable sess, Input: backend to inspect. Output: listed sessions or None after drive failure. (+2 more)

### Community 54 - "Encryption and Rescue Keys"
Cohesion: 0.22
Nodes (6): If you lose both, Rescue-key recovery, Restore with the rescue key, What init prints, What the disc holds, Why scrypt, not Argon2id

### Community 55 - "Verification and Status Docs"
Cohesion: 0.22
Nodes (5): Interrupted sessions, Levels, Post-burn verify, Temporary files, Verify levels

### Community 56 - "Project Development Status"
Cohesion: 0.22
Nodes (9): Current stage, Development notes, Environment-specific hardware limitation (confirmed, not a code defect), Hardening since initial completion, Known issues and limitations, STATE OF THE PROGRAM, Test status, What is done (+1 more)

### Community 57 - "Legacy Format Compatibility"
Cohesion: 0.25
Nodes (8): Discs with no known mode, Encrypted disc metadata, Errors, Files on an encrypted disc, Format 2 discs, Mode detection, Older encrypted discs, Session manifest version 3

### Community 58 - "Backend Identity and Verification"
Cohesion: 0.25
Nodes (8): backend_identity(), Input:  backend - BurnBackend instance     Output: str - canonical identifier (d, _check_verify_coverage(), Input:  backend - disc backend; crypto - manifest decryption backend;, Input:  backend  - BurnBackend to read from             tmp_path - unused. Kept, Input:  backend - disc backend; manifests_by_session - manifests keyed by sessio, _read_all_manifests(), _verify_manifests()

### Community 59 - "Blob Capture and Checking"
Cohesion: 0.25
Nodes (6): _CaptureSource, _check_blobs(), _check_full(), Input:  root - directory _check_blobs captures raw blobs into     Output: N/A (c, Input:  manifest    - Manifest to check             backend     - BurnBackend fo, Input:  manifests   - ascending-session Manifest list already

### Community 60 - "Suspect Manifest Tracking"
Cohesion: 0.50
Nodes (4): _count_suspects_in_range(), _note_suspect_manifests(), Input: disc manifests, first session, disc index, restore session cutoff     Out, Input: disc manifests, marker, disc index, restore session cutoff     Output: fa

### Community 62 - "Browsing and Restore Docs"
Cohesion: 0.29
Nodes (6): Listing, selective restore, and browse, `oddarchiver browse`, `oddarchiver ls`, `oddarchiver restore`, `--path`, `--session`, `--show-deleted`, Recovering a deleted-but-still-present file

### Community 66 - "Media and Hardware Docs"
Cohesion: 0.29
Nodes (5): CD is not supported, Hardware tests versus ISO simulation, M-DISC, Size presets, Supported media

### Community 67 - "Catalog Candidate Resolution"
Cohesion: 0.33
Nodes (6): _disc_excludes_session(), _needed_name(), Input: catalog and requested index. Output: a range-safe disc name., Input: disc range and session. Output: whether the range excludes the session., Input: catalog, unresolved session, and missing discs     Output: next older dis, _unknown_candidate()

### Community 68 - "Command Reference"
Cohesion: 0.33
Nodes (6): Commands, Exit codes, `history`, `verify`, `status`, `init`, `restore`, `sync`

### Community 69 - "Disc Authentication and Loading"
Cohesion: 0.50
Nodes (4): _disc_authenticated(), _initial_manifests(), Input: disc and loaded manifests. Output: whether this disc can be trusted., Input: starting disc, crypto backend, catalog and optional loaded manifests

## Knowledge Gaps
- **144 isolated node(s):** `oddarchiver`, `Overview`, `Goals`, `Non-Goals`, `Constraints` (+139 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `BurnBackend` connect `Burn Backend Interface` to `Session Verification Logic`, `Restore Path Versioning`, `File Encryption and Sessions`, `Manifest and File Hashing`, `Blob Cache Management`, `Software Versioning and Bundling`, `Catalog Persistence and Merging`, `Archive Restore Logic`, `Main CLI Dispatcher`, `Disc Burning and Media`, `Catalog Browsing and Navigation`, `Disc File Extraction`, `CLI Burn Operations`, `Session Delta Processing`, `Session Manifest Management`, `Full Image Extraction`, `Checksum and Failure Reporting`, `Encryption Backend Interface`, `Disc Acquisition and Results`, `ISO Backend Management`, `Volume Manifest and Baseline`, `ISO Path Reading`, `Staging Workspace Management`, `Control Files and NullCrypto`, `Physical Disc Backend`, `Archive Catalog Building`, `Sequential Session Reading`, `Backend Identity and Verification`, `Blob Capture and Checking`, `Disc Authentication and Loading`?**
  _High betweenness centrality (0.046) - this node is a cross-community bridge._
- **Why does `Manifest` connect `Volume Manifest and Baseline` to `Cryptographic Ciphers and KDF`, `Session Verification Logic`, `Restore Path Versioning`, `Manifest and File Hashing`, `Blob Cache Management`, `Disc Encryption Mode Handling`, `Catalog Persistence and Merging`, `Main CLI Dispatcher`, `Catalog Browsing and Navigation`, `Volume Preparation and Reading`, `Disc File Extraction`, `CLI Burn Operations`, `Session Delta Processing`, `Logging and Status Reporting`, `Session Manifest Management`, `Full Image Extraction`, `Checksum and Failure Reporting`, `Encryption Backend Interface`, `Burn Backend Interface`, `Sync Group Staging`, `Staging Workspace Management`, `Control Files and NullCrypto`, `Catalog Event Indexing`, `Disc Crypto Initialization`, `Physical Disc Backend`, `Archive Catalog Building`, `Sequential Session Reading`, `Trailing Session Detection`, `Backend Identity and Verification`, `Blob Capture and Checking`?**
  _High betweenness centrality (0.043) - this node is a cross-community bridge._
- **Why does `CryptoBackend` connect `Encryption Backend Interface` to `Cryptographic Ciphers and KDF`, `Session Verification Logic`, `Restore Path Versioning`, `File Encryption and Sessions`, `Manifest and File Hashing`, `Blob Cache Management`, `Catalog Persistence and Merging`, `Archive Restore Logic`, `Main CLI Dispatcher`, `Catalog Browsing and Navigation`, `CLI Burn Operations`, `Session Delta Processing`, `Session Manifest Management`, `Checksum and Failure Reporting`, `Burn Backend Interface`, `Disc Acquisition and Results`, `Volume Manifest and Baseline`, `Staging Workspace Management`, `Control Files and NullCrypto`, `Disc Crypto Initialization`, `Archive Catalog Building`, `Backend Identity and Verification`, `Blob Capture and Checking`, `Disc Authentication and Loading`?**
  _High betweenness centrality (0.034) - this node is a cross-community bridge._
- **Are the 23 inferred relationships involving `Manifest` (e.g. with `CacheManager` and `ArchiveCatalog`) actually correct?**
  _`Manifest` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Are the 21 inferred relationships involving `CryptoBackend` (e.g. with `CacheManager` and `ArchiveCatalog`) actually correct?**
  _`CryptoBackend` has 21 INFERRED edges - model-reasoned connections that need verification._
- **Are the 25 inferred relationships involving `BurnBackend` (e.g. with `CacheManager` and `ArchiveCatalog`) actually correct?**
  _`BurnBackend` has 25 INFERRED edges - model-reasoned connections that need verification._
- **Are the 15 inferred relationships involving `ManifestEntry` (e.g. with `CryptoBackend` and `KeyfileCrypto`) actually correct?**
  _`ManifestEntry` has 15 INFERRED edges - model-reasoned connections that need verification._