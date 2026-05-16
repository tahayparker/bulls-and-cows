import { describe, expect, it } from "vitest";
import {
  filterCandidates,
  isPlayableWord,
  loadWordList,
  rankGuesses,
  scoreGuess,
  type PlayedGuess,
} from "./solver";

describe("scoreGuess", () => {
  it("counts bulls when the letter and position both match", () => {
    expect(scoreGuess("beam", "bear")).toEqual({ bulls: 3, cows: 0 });
  });

  it("counts cows when a shared unique letter is in the wrong position", () => {
    expect(scoreGuess("beam", "bake")).toEqual({ bulls: 1, cows: 2 });
  });

  it("can return four cows for a full rearrangement", () => {
    expect(scoreGuess("mega", "game")).toEqual({ bulls: 0, cows: 4 });
  });
});

describe("word loading", () => {
  it("keeps only four-letter words with unique letters", () => {
    expect(loadWordList("able aaaa tree BEAM valid")).toEqual(["able", "beam"]);
  });

  it("rejects non-playable words", () => {
    expect(isPlayableWord("tree")).toBe(false);
    expect(isPlayableWord("word")).toBe(true);
    expect(isPlayableWord("words")).toBe(false);
  });
});

describe("candidate filtering", () => {
  const words = ["bake", "beam", "game", "mace"];

  it("keeps only words consistent with previous feedback", () => {
    const guesses: PlayedGuess[] = [
      { id: "1", word: "beam", bulls: 1, cows: 2 },
    ];

    expect(filterCandidates(words, guesses)).toEqual(["bake"]);
  });
});

describe("rankGuesses", () => {
  it("orders recommendations by information value", () => {
    const recommendations = rankGuesses(
      ["bake", "beam", "game", "mace"],
      ["bake", "game", "mace"],
      2,
    );

    expect(recommendations).toHaveLength(2);
    expect(recommendations[0].expectedRemaining).toBeLessThanOrEqual(
      recommendations[1].expectedRemaining,
    );
  });
});
