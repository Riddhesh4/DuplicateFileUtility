import time
import argparse
from duplicate_finder import scanner


def main():
    parser = argparse.ArgumentParser(description="Benchmark duplicate_finder scanner")
    parser.add_argument("paths", nargs="+", help="Paths to scan")
    parser.add_argument("--workers", type=int, default=None, help="Number of workers (None for auto)")
    parser.add_argument("--fast-hash", action="store_true", help="Use xxHash/SHA256 fingerprinting for final file grouping")
    parser.add_argument("--verify-fast-hash", action="store_true", help="Verify fast fingerprint groups with SHA256 after grouping")
    parser.add_argument("--direct-compare", action="store_true", help="Use byte-by-byte file comparison for candidate groups before hashing")
    parser.add_argument("--cache", dest="cache_path", default=None, help="Path to SQLite cache file for file hash metadata")
    args = parser.parse_args()

    if args.verify_fast_hash and not args.fast_hash:
        parser.error("--verify-fast-hash requires --fast-hash")

    t0 = time.time()
    groups = scanner.find_duplicates(
        args.paths,
        workers=args.workers,
        progress_callback=lambda e: None,
        use_fast_hash=args.fast_hash,
        verify_fast_hash=args.verify_fast_hash,
        use_direct_compare=args.direct_compare,
        cache_path=args.cache_path,
    )
    elapsed = time.time() - t0
    print(f"Groups found: {len(groups)}")
    for g in groups[:20]:
        print(g["type"], ":", len(g["files"]), "files, suggested index", g["suggested"]) 
    print("Elapsed:", round(elapsed, 3), "seconds")


if __name__ == "__main__":
    main()
