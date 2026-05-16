"use client";

import { Loader2, Plus, RefreshCcw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  filterCandidates,
  isPlayableWord,
  isPossibleFeedback,
  loadWordList,
  normalizeWord,
  rankGuesses,
  type PlayedGuess,
} from "@/lib/solver";

type LoadState = "loading" | "ready" | "error";

const DEFAULT_MAX_GUESSES = 10;
const MAX_GUESSES_LIMIT = 12;

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

function maxCowsForBulls(bulls: number) {
  return bulls === 3 ? 0 : 4 - bulls;
}

function ReplaceNumberInput({
  ariaLabel,
  value,
  min = 0,
  max,
  disabled = false,
  onChange,
}: {
  ariaLabel: string;
  value: number | "";
  min?: number;
  max: number;
  disabled?: boolean;
  onChange?: (value: number) => void;
}) {
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const maxLength = String(max).length;
  const displayValue = isEditing ? draft : value;

  function updateDraft(rawValue: string) {
    let nextDraft = rawValue.replace(/\D/g, "");

    if (nextDraft.length > maxLength) {
      nextDraft = nextDraft.slice(-maxLength);
    }

    if (nextDraft === "") {
      setDraft("");
      return;
    }

    const nextValue = Number(nextDraft);

    if (!Number.isInteger(nextValue) || nextValue > max) {
      return;
    }

    setDraft(nextDraft);

    if (nextValue >= min) {
      onChange?.(nextValue);
    }
  }

  return (
    <input
      aria-label={ariaLabel}
      type="text"
      inputMode="numeric"
      pattern="[0-9]*"
      disabled={disabled}
      value={displayValue}
      onFocus={(event) => {
        if (!disabled) {
          event.currentTarget.value = "";
          setIsEditing(true);
          setDraft("");
        }
      }}
      onBlur={() => {
        setIsEditing(false);
        setDraft("");
      }}
      onChange={(event) => updateDraft(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.currentTarget.blur();
        }
      }}
    />
  );
}

function TileWord({ word }: { word: string }) {
  return (
    <div className="tile-grid" aria-label={word}>
      {Array.from({ length: 4 }, (_, index) => {
        const letter = word[index] ?? "";

        return (
          <span key={index} className={`word-tile ${letter ? "word-tile-filled" : ""}`}>
            {letter}
          </span>
        );
      })}
    </div>
  );
}

function TileWordInput({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="tile-word-input">
      <span className="sr-only">Guess word</span>
      <input
        className="tile-word-control"
        value={value}
        inputMode="text"
        maxLength={4}
        autoComplete="off"
        onChange={(event) => onChange(normalizeWord(event.target.value))}
      />
      <TileWord word={value} />
    </label>
  );
}

function ScoreInput({
  label,
  value,
  max,
  disabled = false,
  onChange,
}: {
  label: string;
  value: number | "";
  max: number;
  disabled?: boolean;
  onChange?: (value: number) => void;
}) {
  return (
    <label className="score-box">
      <ReplaceNumberInput
        ariaLabel={label}
        min={0}
        max={max}
        disabled={disabled}
        value={value}
        onChange={onChange}
      />
    </label>
  );
}

function PlayedRow({
  item,
  onFeedbackChange,
  onRemove,
}: {
  item: PlayedGuess;
  onFeedbackChange: (id: string, feedback: { bulls?: number; cows?: number }) => void;
  onRemove: (id: string) => void;
}) {
  return (
    <div className="game-row">
      <TileWord word={item.word} />
      <ScoreInput
        label="B"
        value={item.bulls}
        max={4}
        onChange={(bulls) => onFeedbackChange(item.id, { bulls })}
      />
      <ScoreInput
        label="C"
        value={item.cows}
        max={maxCowsForBulls(item.bulls)}
        onChange={(cows) => onFeedbackChange(item.id, { cows })}
      />
      <button
        type="button"
        aria-label={`Remove ${item.word}`}
        className="row-action"
        onClick={() => onRemove(item.id)}
      >
        <Trash2 aria-hidden className="size-4" />
      </button>
    </div>
  );
}

function EmptyRow() {
  return (
    <div className="game-row game-row-empty" aria-hidden>
      <TileWord word="" />
      <ScoreInput label="B" value="" max={4} disabled />
      <ScoreInput label="C" value="" max={4} disabled />
      <span className="row-action" />
    </div>
  );
}

export default function Home() {
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [words, setWords] = useState<string[]>([]);
  const [loadError, setLoadError] = useState("");
  const [guess, setGuess] = useState("");
  const [bulls, setBulls] = useState(0);
  const [cows, setCows] = useState(0);
  const [history, setHistory] = useState<PlayedGuess[]>([]);
  const [maxGuesses, setMaxGuesses] = useState(DEFAULT_MAX_GUESSES);

  useEffect(() => {
    let ignore = false;

    async function loadDictionary() {
      try {
        const response = await fetch("/words.txt", { cache: "no-store" });

        if (!response.ok) {
          throw new Error("Dictionary file not found.");
        }

        const nextWords = loadWordList(await response.text());

        if (!ignore) {
          setWords(nextWords);
          setLoadState(nextWords.length > 0 ? "ready" : "error");
          setLoadError(
            nextWords.length > 0
              ? ""
              : "public/words.txt is empty. Run npm run build:dictionary once.",
          );
        }
      } catch (error) {
        if (!ignore) {
          setLoadState("error");
          setLoadError(error instanceof Error ? error.message : "Could not load words.txt.");
        }
      }
    }

    loadDictionary();

    return () => {
      ignore = true;
    };
  }, []);

  const normalizedGuess = normalizeWord(guess);
  const wordSet = useMemo(() => new Set(words), [words]);
  const candidates = useMemo(() => filterCandidates(words, history), [history, words]);
  const recommendations = useMemo(
    () => rankGuesses(words, candidates, 10),
    [candidates, words],
  );

  const minGuessLimit = Math.max(1, history.length);
  const emptyRows = Math.max(0, maxGuesses - history.length - 1);
  const showActiveRow = history.length < maxGuesses;

  const guessError = useMemo(() => {
    if (!showActiveRow) {
      return "Max guesses reached.";
    }

    if (loadState !== "ready") {
      return "Dictionary is not ready.";
    }

    if (normalizedGuess.length !== 4) {
      return "";
    }

    if (!isPlayableWord(normalizedGuess)) {
      return "Use four different letters.";
    }

    if (!wordSet.has(normalizedGuess)) {
      return "Not in words.txt.";
    }

    if (!isPossibleFeedback({ bulls, cows })) {
      return "Impossible feedback.";
    }

    return "";
  }, [bulls, cows, loadState, normalizedGuess, showActiveRow, wordSet]);

  const canAddGuess =
    normalizedGuess.length === 4 &&
    showActiveRow &&
    loadState === "ready" &&
    !guessError;

  function updateBulls(nextBulls: number) {
    const safeBulls = clamp(nextBulls, 0, 4);
    setBulls(safeBulls);
    setCows((currentCows) => clamp(currentCows, 0, maxCowsForBulls(safeBulls)));
  }

  function updateCows(nextCows: number) {
    setCows(clamp(nextCows, 0, maxCowsForBulls(bulls)));
  }

  function updatePlayedFeedback(
    id: string,
    feedback: {
      bulls?: number;
      cows?: number;
    },
  ) {
    setHistory((current) =>
      current.map((item) => {
        if (item.id !== id) {
          return item;
        }

        const nextBulls = clamp(feedback.bulls ?? item.bulls, 0, 4);
        const nextCows = clamp(
          feedback.cows ?? item.cows,
          0,
          maxCowsForBulls(nextBulls),
        );

        return {
          ...item,
          bulls: nextBulls,
          cows: nextCows,
        };
      }),
    );
  }

  function updateMaxGuesses(nextMaxGuesses: number) {
    setMaxGuesses(clamp(nextMaxGuesses, minGuessLimit, MAX_GUESSES_LIMIT));
  }

  function addGuess() {
    if (!canAddGuess) {
      return;
    }

    setHistory((current) => [
      ...current,
      {
        id: `${normalizedGuess}-${Date.now()}`,
        word: normalizedGuess,
        bulls,
        cows,
      },
    ]);
    setGuess("");
    setBulls(0);
    setCows(0);
  }

  function removeGuess(id: string) {
    setHistory((current) => current.filter((item) => item.id !== id));
  }

  function resetGame() {
    setHistory([]);
    setGuess("");
    setBulls(0);
    setCows(0);
    setMaxGuesses(DEFAULT_MAX_GUESSES);
  }

  return (
    <main className="min-h-screen bg-[#121213] text-[#f8f8f8]">
      <header className="topbar">
        <button type="button" className="icon-button" onClick={resetGame} aria-label="Reset game">
          <RefreshCcw aria-hidden className="size-5" />
        </button>
        <h1>Cows & Bulls</h1>
        <label className="max-guesses">
          <span>Max</span>
          <ReplaceNumberInput
            ariaLabel="Maximum guesses"
            min={minGuessLimit}
            max={MAX_GUESSES_LIMIT}
            value={maxGuesses}
            onChange={updateMaxGuesses}
          />
        </label>
      </header>

      <div className="page-shell">
        <section className="board-shell" aria-label="Game board">
          <div className="game-row column-labels" aria-hidden>
            <span className="tile-column-space" />
            <span>B</span>
            <span>C</span>
            <span />
          </div>

          {history.map((item) => (
            <PlayedRow
              key={item.id}
              item={item}
              onFeedbackChange={updatePlayedFeedback}
              onRemove={removeGuess}
            />
          ))}

          {showActiveRow ? (
            <form
              className="game-row"
              onSubmit={(event) => {
                event.preventDefault();
                addGuess();
              }}
            >
              <TileWordInput value={guess} onChange={setGuess} />
              <ScoreInput label="B" value={bulls} max={4} onChange={updateBulls} />
              <ScoreInput
                label="C"
                value={cows}
                max={maxCowsForBulls(bulls)}
                onChange={updateCows}
              />
              {normalizedGuess.length === 4 ? (
                <button type="submit" className="row-action add-action" disabled={!canAddGuess}>
                  <Plus aria-hidden className="size-5" />
                </button>
              ) : (
                <span className="row-action" aria-hidden />
              )}
            </form>
          ) : null}

          {Array.from({ length: emptyRows }, (_, index) => (
            <EmptyRow key={index} />
          ))}

          <div className="status-line" aria-live="polite">
            {loadState === "loading" ? (
              <span className="loading-inline">
                <Loader2 aria-hidden className="size-4 animate-spin" />
                Loading words
              </span>
            ) : loadState === "error" ? (
              loadError
            ) : guessError ? (
              guessError
            ) : (
              `${candidates.length} possible`
            )}
          </div>
        </section>

        <section className="plain-section" aria-label="Possible words">
          <div className="section-heading">
            <h2>Possible words</h2>
          </div>
          <ol className="word-list">
            {candidates.slice(0, 200).map((word) => (
              <li key={word}>
                <button type="button" onClick={() => setGuess(word)}>
                  {word}
                </button>
              </li>
            ))}
          </ol>
          {candidates.length > 200 ? (
            <p className="small-note">Showing first 200.</p>
          ) : null}
        </section>

        <section className="plain-section" aria-label="Best next guesses">
          <div className="section-heading">
            <h2>Best next guesses</h2>
            <span>{words.length} words</span>
          </div>
          <ol className="guess-list">
            {recommendations.map((item) => (
              <li key={item.word}>
                <button type="button" onClick={() => setGuess(item.word)}>
                  <span>{item.word}</span>
                  <small>
                    expected {item.expectedRemaining.toFixed(1)}, worst{" "}
                    {item.worstCaseRemaining}
                  </small>
                </button>
              </li>
            ))}
          </ol>
        </section>
      </div>
    </main>
  );
}
