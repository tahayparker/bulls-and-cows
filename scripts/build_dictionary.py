from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import permutations
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
RETRY_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
CSV_FIELDNAMES = ["word", "valid", "status", "error", "checked"]
DEFAULT_FLUSH_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL_S = 10

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
RESET  = "\033[0m"


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

_print_lock = Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}min"
    else:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


def fmt_word_result(
    word: str,
    record: dict[str, Any],
    completed: int,
    total: int,
    valid_count: int,
    rate: float,
    eta: float,
    verbose: bool,
) -> str:
    status    = record.get("status", "?")
    is_valid  = record.get("valid") == "true"
    error     = record.get("error", "")
    remaining = total - completed
    idx_w     = len(str(total))

    if is_valid:
        verdict = f"{GREEN}✓ VALID  {RESET}"
    elif error:
        verdict = f"{YELLOW}⚠ ERROR  {RESET}"
    else:
        verdict = f"{RED}✗ invalid{RESET}"

    line = (
        f"[{completed:>{idx_w}}/{total}] "
        f"{CYAN}{word}{RESET}  {verdict}  "
        f"{DIM}HTTP {status:<3}{RESET}"
    )
    if error:
        line += f"  {YELLOW}{error}{RESET}"
    if verbose:
        line += (
            f"  {DIM}│ remaining: {remaining:,}"
            f"  valid: {valid_count:,}"
            f"  {rate:.1f}/s"
            f"  ETA: {fmt_time(eta)}{RESET}"
        )
    return line


def print_banner(
    completed: int,
    total: int,
    valid_count: int,
    elapsed: float,
    rate: float,
    eta: float,
    cache_path: Path,
) -> None:
    pct    = 100.0 * completed / total if total else 0.0
    bar_w  = 32
    filled = int(bar_w * completed / total) if total else 0
    bar    = f"{GREEN}{'█' * filled}{DIM}{'░' * (bar_w - filled)}{RESET}"

    log(
        f"\n{DIM}{'─' * 60}{RESET}\n"
        f"  Progress  [{bar}] {pct:.1f}%\n"
        f"  Checked   {completed:,} / {total:,}   remaining: {total - completed:,}\n"
        f"  Valid     {GREEN}{valid_count:,}{RESET}\n"
        f"  Rate      {rate:.1f} req/s\n"
        f"  Elapsed   {fmt_time(elapsed)}\n"
        f"  ETA       {fmt_time(eta)}\n"
        f"  Cache     {DIM}{cache_path.resolve()}{RESET}\n"
        f"{DIM}{'─' * 60}{RESET}\n"
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a unique-letter four-letter dictionary from DictionaryAPI.dev.",
    )
    parser.add_argument("--out",              default="public/words.txt",            help="Output word list path.")
    parser.add_argument("--cache",            default="data/dictionary-cache.csv",   help="CSV cache path.")
    parser.add_argument("--words-list",       default=None,                          help="File of words to check.")
    parser.add_argument("--dump-words",       action="store_true",                   help="Write valid words to --out after checking.")
    parser.add_argument("--alphabet",         default=DEFAULT_ALPHABET,              help="Letters to permute (default: a-z).")
    parser.add_argument("--workers",          type=int,   default=8,                 help="Concurrent API workers.")
    parser.add_argument("--delay-ms",         type=int,   default=20,                help="Delay between scheduling requests (ms).")
    parser.add_argument("--timeout",          type=float, default=10,                help="Per-request timeout (seconds).")
    parser.add_argument("--retries",          type=int,   default=4,                 help="Retries on transient failures.")
    parser.add_argument("--limit",            type=int,   default=0,                 help="Cap number of words to check (smoke test).")
    parser.add_argument("--progress-every",   type=int,   default=100,               help="Print a summary banner every N completions.")
    parser.add_argument("--flush-every",      type=int,   default=DEFAULT_FLUSH_BATCH_SIZE, help="Flush cache after this many results (1 = immediate).")
    parser.add_argument("--flush-interval",   type=int,   default=DEFAULT_FLUSH_INTERVAL_S, help="Also flush cache every N seconds (0 = disable).")
    parser.add_argument("--verbose",          action="store_true",                   help="Show per-word stats (remaining, rate, ETA).")
    parser.add_argument("--quiet",            action="store_true",                   help="Suppress per-word lines; only show banners.")
    parser.add_argument("--start-over",       action="store_true",                   help="Delete the cache before starting.")
    parser.add_argument("--no-color",         action="store_true",                   help="Disable ANSI colour output.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

def unique_letter_candidates(alphabet: str) -> list[str]:
    letters = sorted({ch.lower() for ch in alphabet if ch.isalpha()})
    if len(letters) < 4:
        raise ValueError("Alphabet must contain at least four unique letters.")
    return ["".join(p) for p in permutations(letters, 4)]


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def load_cache(cache_path: Path) -> dict[str, dict[str, Any]]:
    """
    Read the CSV cache. Later rows overwrite earlier ones (crash-safe /
    append-safe). Stub rows (checked=false, no result) are kept only if no
    real result exists for that word yet.
    """
    if not cache_path.exists():
        return {}

    cache: dict[str, dict[str, Any]] = {}
    with cache_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, fieldnames=CSV_FIELDNAMES)
        for row in reader:
            word = row.get("word")
            if not isinstance(word, str) or word == "word" or len(word) != 4:
                continue
            existing = cache.get(word)
            # Prefer a real result over a stub; otherwise last row wins.
            if existing and existing.get("checked") == "true" and row.get("checked") != "true":
                continue
            cache[word] = dict(row)
    return cache


def compact_cache(cache_path: Path, cache: dict[str, dict[str, Any]]) -> int:
    """
    Rewrite the CSV as a clean, sorted, deduplicated file.
    Returns the number of rows written.
    Skips pure stub rows (checked=false, no status) — they are useless noise
    left over from the old initialize_csv_cache call.
    """
    rows = []
    for word in sorted(cache):
        rec = cache[word]
        if rec.get("checked") != "true" and not rec.get("status") and not rec.get("valid"):
            continue
        rows.append(rec)

    with cache_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for rec in rows:
            writer.writerow({k: rec.get(k, "") for k in CSV_FIELDNAMES})

    return len(rows)

class CacheWriter:
    """
    Thread-safe, append-only CSV writer.
    Flushes to disk when EITHER:
      • buffer reaches `flush_every` records, OR
      • `flush_interval` seconds have elapsed since last flush.
    """

    def __init__(self, cache_path: Path, flush_every: int, flush_interval: float) -> None:
        self._path           = cache_path
        self._flush_every    = flush_every
        self._flush_interval = flush_interval
        self._buffer: list[dict[str, Any]] = []
        self._lock           = Lock()
        self._closed         = False
        self._total_flushed  = 0

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists():
            with cache_path.open("w", encoding="utf-8", newline="") as fh:
                csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES).writeheader()

        if flush_interval > 0:
            Thread(target=self._timer_loop, daemon=True).start()

    def write(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._buffer.append(record)
            if len(self._buffer) >= self._flush_every:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        count = len(self._buffer)
        with self._path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
            for rec in self._buffer:
                writer.writerow({k: rec.get(k, "") for k in CSV_FIELDNAMES})
        self._buffer.clear()
        self._total_flushed += count
        log(
            f"  {DIM}[cache] flushed {count} record(s) to disk  "
            f"(total saved: {self._total_flushed:,})  "
            f"→ {self._path.resolve()}{RESET}"
        )

    def _timer_loop(self) -> None:
        while not self._closed:
            time.sleep(self._flush_interval)
            with self._lock:
                if self._buffer:
                    self._flush_locked()

    def close(self) -> None:
        self._closed = True
        with self._lock:
            self._flush_locked()


# ---------------------------------------------------------------------------
# Output word list
# ---------------------------------------------------------------------------

def write_words(out_path: Path, cache: dict[str, dict[str, Any]]) -> list[str]:
    valid_words = sorted(
        word for word, rec in cache.items()
        if rec.get("valid") in ("true", True)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text("\n".join(valid_words) + "\n", encoding="utf-8")
    tmp.replace(out_path)
    return valid_words


# ---------------------------------------------------------------------------
# API request
# ---------------------------------------------------------------------------

def parse_success(body: bytes) -> bool:
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return False
    return isinstance(data, list) and any(
        isinstance(e, dict) and e.get("word") for e in data
    )


def check_word(word: str, timeout: float, retries: int) -> dict[str, Any]:
    url = API_URL.format(word=quote(word))
    last_error = ""

    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={
                "Accept":     "application/json",
                "User-Agent": "bulls-and-cows-dictionary-builder/1.0",
            })
            with urlopen(req, timeout=timeout) as resp:
                body   = resp.read()
                status = resp.status

            is_valid = status == 200 and parse_success(body)
            return {
                "word":    word,
                "valid":   "true" if is_valid else "false",
                "status":  str(status),
                "error":   "",
                "checked": "true",
            }

        except HTTPError as exc:
            status = exc.code
            if status == 404:
                return {"word": word, "valid": "false", "status": "404",
                        "error": "", "checked": "true"}
            last_error = f"HTTP {status}"
            if status not in RETRY_HTTP_STATUSES or attempt == retries:
                return {"word": word, "valid": "false", "status": str(status),
                        "error": last_error, "checked": "true"}

        except (TimeoutError, URLError) as exc:
            last_error = str(exc)
            if attempt == retries:
                return {"word": word, "valid": "false", "status": "0",
                        "error": last_error, "checked": "true"}

        backoff = min(30, 2 ** attempt)
        log(f"  {YELLOW}↻ retry {attempt + 1}/{retries} for '{word}'"
            f" in {backoff}s  ({last_error}){RESET}")
        time.sleep(backoff)

    return {"word": word, "valid": "false", "status": "0",
            "error": last_error, "checked": "true"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    if args.no_color or not sys.stdout.isatty():
        global GREEN, RED, YELLOW, CYAN, DIM, RESET
        GREEN = RED = YELLOW = CYAN = DIM = RESET = ""

    cache_path = Path(args.cache)
    out_path   = Path(args.out)

    if args.workers < 1:
        raise ValueError("--workers must be at least 1.")

    log(f"\n{'═' * 60}")
    log(f"  Bulls & Cows  —  Dictionary Builder")
    log(f"{'═' * 60}")
    log(f"  Cache (absolute)  {cache_path.resolve()}")
    log(f"  Output            {out_path.resolve()}")
    log(f"  Workers           {args.workers}")
    log(f"  Delay             {args.delay_ms} ms")
    log(f"  Timeout           {args.timeout} s")
    log(f"  Retries           {args.retries}")
    log(f"  Flush every       {args.flush_every} result(s)")
    if args.flush_interval > 0:
        log(f"  Flush interval    {args.flush_interval}s")
    if args.words_list:
        log(f"  Words file        {Path(args.words_list).resolve()}")
    log(f"{'═' * 60}\n")

    if args.start_over and cache_path.exists():
        cache_path.unlink()
        log(f"[info] Deleted existing cache.")

    # ── Candidates ─────────────────────────────────────────────────────────
    candidates    = unique_letter_candidates(args.alphabet)
    candidate_set = set(candidates)
    log(f"[info] Unique-letter 4-letter candidates : {len(candidates):,}")

    # ── Load + compact cache ───────────────────────────────────────────────
    cache = load_cache(cache_path)

    raw_count = len(cache)
    if cache_path.exists():
        # Count raw rows to detect bloat
        with cache_path.open("r", encoding="utf-8", newline="") as fh:
            raw_rows = sum(1 for _ in fh) - 1  # subtract header
        if raw_rows > raw_count:
            log(
                f"[info] Cache has {raw_rows:,} rows for {raw_count:,} unique words "
                f"({raw_rows - raw_count:,} duplicates/stubs) — compacting..."
            )
            written = compact_cache(cache_path, cache)
            log(f"[info] Compacted to {written:,} clean rows (sorted, deduplicated).")
        else:
            log(f"[info] Cache loaded: {raw_count:,} entries (clean, no compaction needed).")
    else:
        log(f"[info] No existing cache found — starting fresh.")

    in_set  = sum(1 for w in cache if w in candidate_set)
    outside = len(cache) - in_set
    if outside:
        log(
            f"[info] {outside:,} cached word(s) are outside the candidate set\n"
            f"       (repeated-letter words from --words-list — harmless)."
        )

    # ── Pending words — errored first, then unchecked ──────────────────────
    if args.words_list:
        wl_path = Path(args.words_list)
        if not wl_path.exists():
            log(f"{YELLOW}[warn] --words-list not found; using permutation candidates.{RESET}")
            pending_source = candidates
        else:
            with wl_path.open("r", encoding="utf-8") as fh:
                pending_source = [w.strip().lower() for w in fh if w.strip()]
            log(f"[info] Words loaded from file             : {len(pending_source):,}")
    else:
        pending_source = candidates

    errored   = [
        w for w in pending_source
        if cache.get(w, {}).get("checked") == "true" and cache.get(w, {}).get("error")
    ]
    unchecked = [
        w for w in pending_source
        if cache.get(w, {}).get("checked") != "true"
    ]
    pending      = errored + unchecked
    already_done = len(pending_source) - len(pending)

    if args.limit > 0:
        pending = pending[: args.limit]
        log(f"[info] --limit applied: capped at {args.limit:,}")

    log(f"[info] Already checked (skipping)        : {already_done:,}")
    log(f"[info] Errored (will retry first)         : {YELLOW}{len(errored):,}{RESET}")
    log(f"[info] Unchecked (new)                    : {len(unchecked):,}")
    log(f"[info] Total to check this run            : {len(pending):,}\n")

    if not pending:
        log("[info] Nothing to check.")
        if args.dump_words:
            valid_words = write_words(out_path, cache)
            log(f"[info] Wrote {len(valid_words):,} valid words → {out_path.resolve()}")
        return 0

    # ── Run ────────────────────────────────────────────────────────────────
    cache_writer = CacheWriter(
        cache_path,
        flush_every=args.flush_every,
        flush_interval=args.flush_interval,
    )
    delay_s = max(args.delay_ms, 0) / 1000.0

    completed          = 0
    scheduled          = 0
    valid_count        = sum(1 for r in cache.values() if r.get("valid") == "true")
    futures: dict[Any, str] = {}
    pending_iter       = iter(pending)
    started_at         = time.monotonic()
    last_schedule_time = time.monotonic()

    log(f"[info] Starting.  (already valid in cache: {valid_count:,})\n")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:

        def schedule_next() -> None:
            nonlocal scheduled, last_schedule_time
            try:
                word = next(pending_iter)
            except StopIteration:
                return
            futures[executor.submit(check_word, word, args.timeout, args.retries)] = word
            scheduled += 1
            last_schedule_time = time.monotonic()
            if args.verbose and not args.quiet:
                log(f"  {DIM}→ queued '{word}'  "
                    f"(in-flight: {len(futures)}, scheduled: {scheduled}/{len(pending)}){RESET}")

        for _ in range(args.workers):
            schedule_next()

        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED, timeout=1.0)

            for future in done:
                word   = futures.pop(future)
                record = future.result()
                cache[word] = record
                cache_writer.write(record)
                completed += 1

                if record.get("valid") == "true":
                    valid_count += 1

                elapsed   = max(time.monotonic() - started_at, 1e-6)
                rate      = completed / elapsed
                remaining = len(pending) - completed
                eta       = remaining / rate if rate > 0 else 0.0

                if not args.quiet:
                    log(fmt_word_result(
                        word, record,
                        completed, len(pending), valid_count,
                        rate, eta, args.verbose,
                    ))

                if completed % args.progress_every == 0 or completed == len(pending):
                    print_banner(completed, len(pending), valid_count,
                                 elapsed, rate, eta, cache_path)

            now = time.monotonic()
            while scheduled < len(pending) and (now - last_schedule_time) >= delay_s:
                schedule_next()
                now = time.monotonic()

    cache_writer.close()

    # Compact on exit so the file is always clean for next run
    log(f"\n[info] Compacting cache before exit...")
    written = compact_cache(cache_path, cache)
    log(f"[info] Cache compacted to {written:,} rows → {cache_path.resolve()}")

    elapsed_total = time.monotonic() - started_at
    log(f"\n{'═' * 60}")
    log(f"  Run complete")
    log(f"{'═' * 60}")
    log(f"  Words checked  {completed:,}")
    log(f"  Valid found    {GREEN}{valid_count:,}{RESET}")
    log(f"  Elapsed        {fmt_time(elapsed_total)}")
    log(f"  Avg rate       {completed / elapsed_total:.1f} req/s")
    log(f"  Cache          {cache_path.resolve()}")
    if args.dump_words:
        valid_words = write_words(out_path, cache)
        log(f"  Word list      {len(valid_words):,} words → {out_path.resolve()}")
    log(f"{'═' * 60}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())