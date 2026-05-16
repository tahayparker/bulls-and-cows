# Bulls & Cows Solver

A simple Next.js dashboard for four-letter Bulls and Cows with unique letters.

The dashboard never calls the Dictionary API during play. It loads `public/words.txt`,
filters possible answers locally, and ranks next guesses by expected information gain.

## Commands

```bash
npm run dev
npm run build
npm run lint
npm run test
```

## Build the Dictionary

Generate the one-time API-backed word list:

```bash
npm run build:dictionary
```

That command checks every four-letter `a-z` candidate with unique letters against:

```text
https://api.dictionaryapi.dev/api/v2/entries/en/<word>
```

Results are cached in `data/dictionary-cache.jsonl`, so the scan can be stopped and
resumed without repeating completed API calls. The final accepted words are written to
`public/words.txt`, which is what the dashboard uses.

For a small smoke test:

```bash
python scripts/build_dictionary.py --limit 50 --out data/smoke-words.txt --cache data/smoke-cache.jsonl
```

## Game Rules

- Four lowercase letters per word.
- Letters must be unique.
- Bulls are correct letters in the correct position.
- Cows are correct letters in the wrong position.
- Misses are shown as `X`.
