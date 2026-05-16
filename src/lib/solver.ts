export type Feedback = {
  bulls: number;
  cows: number;
};

export type PlayedGuess = Feedback & {
  id: string;
  word: string;
};

export type GuessRecommendation = {
  word: string;
  expectedRemaining: number;
  worstCaseRemaining: number;
  partitions: number;
  isPossibleAnswer: boolean;
};

const FOUR_LOWERCASE_LETTERS = /^[a-z]{4}$/;

export function normalizeWord(value: string) {
  return value.toLowerCase().replace(/[^a-z]/g, "").slice(0, 4);
}

export function hasUniqueLetters(word: string) {
  return new Set(word).size === word.length;
}

export function isPlayableWord(word: string) {
  return FOUR_LOWERCASE_LETTERS.test(word) && hasUniqueLetters(word);
}

export function scoreGuess(guess: string, secret: string): Feedback {
  if (!isPlayableWord(guess) || !isPlayableWord(secret)) {
    throw new Error("scoreGuess expects two four-letter words with unique letters.");
  }

  let bulls = 0;
  let sharedLetters = 0;
  const secretLetters = new Set(secret);

  for (let index = 0; index < 4; index += 1) {
    if (guess[index] === secret[index]) {
      bulls += 1;
    }

    if (secretLetters.has(guess[index])) {
      sharedLetters += 1;
    }
  }

  return {
    bulls,
    cows: sharedLetters - bulls,
  };
}

export function feedbackKey(feedback: Feedback) {
  return `${feedback.bulls}:${feedback.cows}`;
}

export function isPossibleFeedback(feedback: Feedback) {
  return (
    Number.isInteger(feedback.bulls) &&
    Number.isInteger(feedback.cows) &&
    feedback.bulls >= 0 &&
    feedback.cows >= 0 &&
    feedback.bulls + feedback.cows <= 4 &&
    feedbackKey(feedback) !== "3:1"
  );
}

export function loadWordList(text: string) {
  const seen = new Set<string>();

  for (const rawWord of text.split(/\s+/)) {
    const word = rawWord.trim().toLowerCase();

    if (isPlayableWord(word)) {
      seen.add(word);
    }
  }

  return Array.from(seen).sort();
}

export function filterCandidates(words: string[], guesses: PlayedGuess[]) {
  if (guesses.length === 0) {
    return words;
  }

  return words.filter((secret) =>
    guesses.every((guess) => {
      const score = scoreGuess(guess.word, secret);

      return score.bulls === guess.bulls && score.cows === guess.cows;
    }),
  );
}

export function rankGuesses(
  words: string[],
  candidates: string[],
  limit = 12,
): GuessRecommendation[] {
  if (words.length === 0 || candidates.length === 0) {
    return [];
  }

  const possibleAnswers = new Set(candidates);
  const candidateCount = candidates.length;

  return words
    .map((word) => {
      const buckets = new Map<string, number>();

      for (const candidate of candidates) {
        const key = feedbackKey(scoreGuess(word, candidate));
        buckets.set(key, (buckets.get(key) ?? 0) + 1);
      }

      let weightedRemaining = 0;
      let worstCaseRemaining = 0;

      for (const size of buckets.values()) {
        weightedRemaining += size * size;
        worstCaseRemaining = Math.max(worstCaseRemaining, size);
      }

      return {
        word,
        expectedRemaining: weightedRemaining / candidateCount,
        worstCaseRemaining,
        partitions: buckets.size,
        isPossibleAnswer: possibleAnswers.has(word),
      };
    })
    .sort((left, right) => {
      if (left.expectedRemaining !== right.expectedRemaining) {
        return left.expectedRemaining - right.expectedRemaining;
      }

      if (left.worstCaseRemaining !== right.worstCaseRemaining) {
        return left.worstCaseRemaining - right.worstCaseRemaining;
      }

      if (left.isPossibleAnswer !== right.isPossibleAnswer) {
        return left.isPossibleAnswer ? -1 : 1;
      }

      return left.word.localeCompare(right.word);
    })
    .slice(0, limit);
}
