from __future__ import annotations

import argparse
import asyncio
import csv
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from tqdm import tqdm

API_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
RETRY_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
CSV_FIELDNAMES = ["word", "valid", "status", "error", "checked", "checked_at"]
DEFAULT_FLUSH_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL_S = 10

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

USE_COLOR = True

def _c(code: str, text: str) -> str:
    return f"{code}{text}\033[0m" if USE_COLOR else text

def green(t: str)  -> str: return _c("\033[92m", t)
def red(t: str)    -> str: return _c("\033[91m", t)
def yellow(t: str) -> str: return _c("\033[93m", t)
def cyan(t: str)   -> str: return _c("\033[96m", t)
def dim(t: str)    -> str: return _c("\033[2m",  t)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def has_unique_letters(word: str) -> bool:
    """Return True only if every character in the word is unique."""
    return len(set(word)) == len(word)

def is_valid_candidate(word: str) -> bool:
    """4-letter, alpha-only, all unique letters."""
    return len(word) == 4 and word.isalpha() and has_unique_letters(word)


# ---------------------------------------------------------------------------
# Candidate generation  (permutations — always unique by definition)
# ---------------------------------------------------------------------------

def unique_letter_candidates(alphabet: str) -> list[str]:
    from itertools import permutations
    letters = sorted({ch.lower() for ch in alphabet if ch.isalpha()})
    if len(letters) < 4:
        raise ValueError("Alphabet must contain at least four unique letters.")
    # permutations never repeats an element — unique letters guaranteed
    return ["".join(p) for p in permutations(letters, 4)]


# ---------------------------------------------------------------------------
# Logging / formatting
# ---------------------------------------------------------------------------

def fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}min"
    else:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a unique-letter four-letter dictionary from DictionaryAPI.dev.",
    )
    parser.add_argument("--out",                  default="public/words.txt",             help="Output word list path.")
    parser.add_argument("--cache",                default="data/dictionary-cache.csv",    help="CSV cache path.")
    parser.add_argument("--candidate-list",       default="data/candidate_list.txt",      help="Candidate tracking file (popped as words are checked).")
    parser.add_argument("--words-list",           default=None,                           help="File of words to check (must be 4-letter, unique-letter).")
    parser.add_argument("--dump-words",           action="store_true",                    help="Write valid words to --out after checking.")
    parser.add_argument("--alphabet",             default=DEFAULT_ALPHABET,               help="Letters to permute (default: a-z).")
    parser.add_argument("--workers",              type=int,   default=32,                 help="Concurrent async workers.")
    parser.add_argument("--delay-ms",             type=int,   default=20,                 help="Delay between scheduling requests (ms).")
    parser.add_argument("--timeout",              type=float, default=10,                 help="Per-request timeout (seconds).")
    parser.add_argument("--retries",              type=int,   default=4,                  help="Retries on transient failures.")
    parser.add_argument("--limit",                type=int,   default=0,                  help="Cap number of words to check (smoke test).")
    parser.add_argument("--progress-every",       type=int,   default=100,                help="Print a summary banner every N completions.")
    parser.add_argument("--flush-every",          type=int,   default=DEFAULT_FLUSH_BATCH_SIZE, help="Flush cache after this many results.")
    parser.add_argument("--flush-interval",       type=int,   default=DEFAULT_FLUSH_INTERVAL_S, help="Also flush cache every N seconds (0 = disable).")
    parser.add_argument("--retry-errors-older-than", type=int, default=0,                help="Re-check errored words cached more than N days ago (0 = all errors).")
    parser.add_argument("--verbose",              action="store_true",                    help="Show per-word stats.")
    parser.add_argument("--quiet",                action="store_true",                    help="Suppress per-word lines; only show progress bar.")
    parser.add_argument("--start-over",           action="store_true",                    help="Delete the cache before starting.")
    parser.add_argument("--no-color",             action="store_true",                    help="Disable ANSI colour output.")
    parser.add_argument("--fallback-to-permutations", action="store_true",               help="Fall back to permutation candidates if --words-list is missing.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def load_cache(cache_path: Path) -> dict[str, dict[str, Any]]:
    if not cache_path.exists():
        return {}

    cache: dict[str, dict[str, Any]] = {}
    with cache_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            word = row.get("word", "").strip().lower()
            # Hard filter: skip any word that isn't 4-letter unique-letter alpha
            if not is_valid_candidate(word):
                continue
            existing = cache.get(word)
            if existing and existing.get("checked") == "true" and row.get("checked") != "true":
                continue
            cache[word] = dict(row)
    return cache


def purge_double_letter_rows(cache_path: Path) -> int:
    """
    Remove any rows from the CSV whose word has repeated letters.
    Returns the number of rows removed.
    """
    if not cache_path.exists():
        return 0

    kept: list[dict] = []
    removed = 0
    with cache_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or CSV_FIELDNAMES
        for row in reader:
            word = row.get("word", "").strip().lower()
            if not is_valid_candidate(word):
                removed += 1
            else:
                kept.append(row)

    if removed:
        with cache_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)

    return removed


def compact_cache(cache_path: Path, cache: dict[str, dict[str, Any]]) -> int:
    rows = []
    for word in sorted(cache):
        rec = cache[word]
        # Skip stubs and any double-letter words that slipped in
        if not is_valid_candidate(word):
            continue
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
    """Async-safe append-only CSV writer with batch + interval flushing."""

    def __init__(self, cache_path: Path, flush_every: int, flush_interval: float) -> None:
        self._path           = cache_path
        self._flush_every    = flush_every
        self._flush_interval = flush_interval
        self._buffer: list[dict[str, Any]] = []
        self._lock           = asyncio.Lock()
        self._closed         = False
        self._total_flushed  = 0
        self._timer_task: asyncio.Task | None = None

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists():
            with cache_path.open("w", encoding="utf-8", newline="") as fh:
                csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES).writeheader()

    def start_timer(self) -> None:
        if self._flush_interval > 0:
            self._timer_task = asyncio.create_task(self._timer_loop())

    async def write(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self._buffer.append(record)
            if len(self._buffer) >= self._flush_every:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return
        count = len(self._buffer)
        loop = asyncio.get_event_loop()
        buf_snapshot = list(self._buffer)
        self._buffer.clear()
        path = self._path

        def _write():
            with path.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
                for rec in buf_snapshot:
                    writer.writerow({k: rec.get(k, "") for k in CSV_FIELDNAMES})

        await loop.run_in_executor(None, _write)
        self._total_flushed += count

    async def _timer_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._flush_interval)
            async with self._lock:
                if self._buffer:
                    await self._flush_locked()

    async def close(self) -> None:
        self._closed = True
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            await self._flush_locked()


# ---------------------------------------------------------------------------
# Candidate list tracker
# ---------------------------------------------------------------------------

class CandidateTracker:
    """
    Maintains a file of unchecked candidates.
    Each word is removed (popped) from the file once it has been checked.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def initialise(self, candidates: list[str]) -> None:
        """Write initial candidate list if the file doesn't exist."""
        if self._path.exists():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("\n".join(sorted(candidates)) + "\n", encoding="utf-8")

    def load_remaining(self) -> set[str]:
        if not self._path.exists():
            return set()
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return {w.strip().lower() for w in lines if w.strip()}

    async def pop(self, word: str) -> None:
        """Remove a word from the candidate file (it has been checked)."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            path = self._path

            def _remove():
                if not path.exists():
                    return
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                with path.open("w", encoding="utf-8") as fh:
                    for line in lines:
                        if line.strip().lower() != word:
                            fh.write(line)

            await loop.run_in_executor(None, _remove)


# ---------------------------------------------------------------------------
# Output word list
# ---------------------------------------------------------------------------

def write_words(out_path: Path, cache: dict[str, dict[str, Any]]) -> list[str]:
    valid_words = sorted(
        word for word, rec in cache.items()
        if rec.get("valid") in ("true", True) and is_valid_candidate(word)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text("\n".join(valid_words) + "\n", encoding="utf-8")
    tmp.replace(out_path)
    return valid_words


# ---------------------------------------------------------------------------
# Adaptive rate limiter (token bucket)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, rate: float) -> None:
        self._rate     = rate        # tokens/sec
        self._tokens   = rate
        self._last     = time.monotonic()
        self._lock     = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last   = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1

    def backpressure(self) -> None:
        """Called on 429 — halve the rate."""
        self._rate = max(0.5, self._rate * 0.5)

    def recover(self) -> None:
        """Called on success — nudge rate back up."""
        self._rate = min(50.0, self._rate * 1.05)


# ---------------------------------------------------------------------------
# API request  (async)
# ---------------------------------------------------------------------------

def parse_success(body: str) -> bool:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(data, list) and any(
        isinstance(e, dict) and e.get("word") for e in data
    )


async def check_word(
    session: aiohttp.ClientSession,
    word: str,
    timeout: float,
    retries: int,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    url = API_URL.format(word=word)
    last_error = ""

    for attempt in range(retries + 1):
        await rate_limiter.acquire()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                body   = await resp.text()
                status = resp.status

            if status == 200:
                rate_limiter.recover()
                return {
                    "word":       word,
                    "valid":      "true" if parse_success(body) else "false",
                    "status":     "200",
                    "error":      "",
                    "checked":    "true",
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            if status == 404:
                rate_limiter.recover()
                return {"word": word, "valid": "false", "status": "404",
                        "error": "", "checked": "true",
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

            last_error = f"HTTP {status}"
            if status == 429:
                rate_limiter.backpressure()
            if status not in RETRY_HTTP_STATUSES or attempt == retries:
                return {"word": word, "valid": "false", "status": str(status),
                        "error": last_error, "checked": "true",
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = str(exc)
            if attempt == retries:
                return {"word": word, "valid": "false", "status": "0",
                        "error": last_error, "checked": "true",
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

        backoff = min(30, 2 ** attempt)
        await asyncio.sleep(backoff)

    return {"word": word, "valid": "false", "status": "0",
            "error": last_error, "checked": "true",
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_shutdown = False

def _handle_signal(cache_writer_ref: list) -> None:
    global _shutdown
    _shutdown = True
    print("\n[signal] Interrupt received — flushing cache before exit...", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def async_main(args: argparse.Namespace) -> int:
    global USE_COLOR
    if args.no_color or not sys.stdout.isatty():
        USE_COLOR = False

    cache_path     = Path(args.cache)
    out_path       = Path(args.out)
    candidate_path = Path(args.candidate_list)

    if args.workers < 1:
        raise ValueError("--workers must be at least 1.")

    print(f"\n{'═' * 60}")
    print(f"  Cows & Bulls  —  Dictionary Builder")
    print(f"{'═' * 60}")
    print(f"  Cache           {cache_path.resolve()}")
    print(f"  Output          {out_path.resolve()}")
    print(f"  Candidate list  {candidate_path.resolve()}")
    print(f"  Workers         {args.workers}")
    print(f"  Timeout         {args.timeout}s")
    print(f"  Retries         {args.retries}")
    print(f"{'═' * 60}\n")

    if args.start_over:
        for p in (cache_path, candidate_path):
            if p.exists():
                p.unlink()
                print(f"[info] Deleted {p}")

    # ── Candidates ─────────────────────────────────────────────────────────
    candidates    = unique_letter_candidates(args.alphabet)
    candidate_set = set(candidates)
    print(f"[info] Unique-letter 4-letter candidates: {len(candidates):,}")

    # ── Purge double-letter rows from existing cache ────────────────────────
    removed = purge_double_letter_rows(cache_path)
    if removed:
        print(f"[info] Purged {removed:,} double-letter row(s) from cache.")

    # ── Load + compact cache ───────────────────────────────────────────────
    cache = load_cache(cache_path)
    raw_count = len(cache)
    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8", newline="") as fh:
            raw_rows = sum(1 for _ in fh) - 1
        if raw_rows > raw_count:
            print(f"[info] Compacting cache ({raw_rows - raw_count:,} duplicates/stubs)...")
            written = compact_cache(cache_path, cache)
            print(f"[info] Compacted to {written:,} rows.")
        else:
            print(f"[info] Cache: {raw_count:,} entries (clean).")
    else:
        print("[info] No existing cache — starting fresh.")

    # ── Candidate tracker ──────────────────────────────────────────────────
    tracker = CandidateTracker(candidate_path)
    tracker.initialise(candidates)

    # ── Pending words ──────────────────────────────────────────────────────
    if args.words_list:
        wl_path = Path(args.words_list)
        if not wl_path.exists():
            if not args.fallback_to_permutations:
                print(f"[error] --words-list '{wl_path}' not found. "
                      f"Pass --fallback-to-permutations to use generated candidates instead.")
                return 1
            print(yellow("[warn] --words-list not found; falling back to permutation candidates."))
            pending_source = candidates
        else:
            raw_words = [w.strip().lower() for w in wl_path.read_text("utf-8").splitlines() if w.strip()]
            # Deduplicate (preserve order)
            seen: set[str] = set()
            deduped: list[str] = []
            for w in raw_words:
                if w not in seen:
                    seen.add(w)
                    deduped.append(w)
            # Hard filter: only 4-letter unique-letter alpha words
            pending_source = [w for w in deduped if is_valid_candidate(w)]
            skipped = len(raw_words) - len(pending_source)
            if skipped:
                print(f"[info] Skipped {skipped:,} word(s) from file "
                      f"(wrong length, repeated letters, or non-alpha).")
            print(f"[info] Words from file (valid candidates): {len(pending_source):,}")
    else:
        pending_source = candidates

    # Error age filter
    now_ts = time.time()
    cutoff_days = args.retry_errors_older_than

    def should_retry_error(rec: dict) -> bool:
        if cutoff_days <= 0:
            return True  # retry all errors
        checked_at_str = rec.get("checked_at", "")
        if not checked_at_str:
            return True
        try:
            import datetime
            t = datetime.datetime.strptime(checked_at_str, "%Y-%m-%dT%H:%M:%SZ")
            age_days = (datetime.datetime.utcnow() - t).days
            return age_days >= cutoff_days
        except ValueError:
            return True

    errored   = [
        w for w in pending_source
        if cache.get(w, {}).get("checked") == "true"
        and cache.get(w, {}).get("error")
        and should_retry_error(cache.get(w, {}))
    ]
    unchecked = [
        w for w in pending_source
        if cache.get(w, {}).get("checked") != "true"
    ]
    pending      = errored + unchecked
    already_done = len(pending_source) - len(pending)

    if args.limit > 0:
        pending = pending[: args.limit]
        print(f"[info] --limit applied: {args.limit:,}")

    print(f"[info] Already checked (skipping):  {already_done:,}")
    print(f"[info] Errored (retrying first):    {yellow(str(len(errored)))}")
    print(f"[info] Unchecked (new):             {len(unchecked):,}")
    print(f"[info] Total to check this run:     {len(pending):,}\n")

    if not pending:
        print("[info] Nothing to check.")
        if args.dump_words:
            valid_words = write_words(out_path, cache)
            print(f"[info] Wrote {len(valid_words):,} valid words → {out_path.resolve()}")
        return 0

    # ── Setup ──────────────────────────────────────────────────────────────
    cache_writer  = CacheWriter(cache_path, args.flush_every, args.flush_interval)
    rate_limiter  = RateLimiter(rate=1000.0 / max(args.delay_ms, 1))
    valid_count   = sum(1 for r in cache.values() if r.get("valid") == "true")
    completed     = 0
    started_at    = time.monotonic()

    cache_writer_ref: list = [cache_writer]

    loop = asyncio.get_event_loop()
    # Signal handlers are not supported on Windows
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT,  lambda: _handle_signal(cache_writer_ref))
        loop.add_signal_handler(signal.SIGTERM, lambda: _handle_signal(cache_writer_ref))

    cache_writer.start_timer()

    pbar = tqdm(
        total=len(pending),
        unit="word",
        disable=args.quiet,
        dynamic_ncols=True,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
    )

    sem = asyncio.Semaphore(args.workers)

    async def bounded_check(session: aiohttp.ClientSession, word: str) -> None:
        nonlocal completed, valid_count
        if _shutdown:
            return
        async with sem:
            record = await check_word(session, word, args.timeout, args.retries, rate_limiter)

        cache[word] = record
        await cache_writer.write(record)
        await tracker.pop(word)
        completed += 1

        if record.get("valid") == "true":
            valid_count += 1

        if not args.quiet:
            status   = record.get("status", "?")
            is_valid = record.get("valid") == "true"
            error    = record.get("error", "")
            if is_valid:
                verdict = green("✓ VALID  ")
            elif error:
                verdict = yellow("⚠ ERROR  ")
            else:
                verdict = red("✗ invalid")

            line = f"[{completed:>{len(str(len(pending)))}}/{len(pending)}] {cyan(word)}  {verdict}  {dim(f'HTTP {status:<3}')}"
            if error:
                line += f"  {yellow(error)}"
            if args.verbose:
                elapsed = max(time.monotonic() - started_at, 1e-6)
                rate    = completed / elapsed
                eta     = (len(pending) - completed) / rate if rate > 0 else 0
                line   += f"  {dim(f'valid: {valid_count:,}  {rate:.1f}/s  ETA: {fmt_time(eta)}')}"
            tqdm.write(line)

        pbar.update(1)
        pbar.set_postfix(valid=valid_count, rate_limit=f"{rate_limiter._rate:.1f}/s")

    connector = aiohttp.TCPConnector(limit=args.workers)
    headers   = {
        "Accept":     "application/json",
        "User-Agent": "bulls-and-cows-dictionary-builder/2.0",
    }

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        tasks = [asyncio.create_task(bounded_check(session, w)) for w in pending]
        await asyncio.gather(*tasks, return_exceptions=True)

    pbar.close()
    await cache_writer.close()

    # Compact on exit
    print(f"\n[info] Compacting cache...")
    written = compact_cache(cache_path, cache)
    print(f"[info] Cache compacted to {written:,} rows → {cache_path.resolve()}")

    elapsed_total = time.monotonic() - started_at
    print(f"\n{'═' * 60}")
    print(f"  Run complete")
    print(f"{'═' * 60}")
    print(f"  Words checked  {completed:,}")
    print(f"  Valid found    {green(str(valid_count))}")
    print(f"  Elapsed        {fmt_time(elapsed_total)}")
    print(f"  Avg rate       {completed / max(elapsed_total, 1e-6):.1f} req/s")
    print(f"  Cache          {cache_path.resolve()}")
    print(f"  Candidates     {candidate_path.resolve()}")

    if args.dump_words:
        valid_words = write_words(out_path, cache)
        print(f"  Word list      {len(valid_words):,} words → {out_path.resolve()}")

    print(f"{'═' * 60}\n")
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())