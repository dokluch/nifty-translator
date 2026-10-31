# Nifty Translator

Turns a scanned or already-OCRed book into a checked translation in any language. There is no UI: each book is a folder with one settings file, and everything runs from the command line.

Pages are read through OpenRouter. Every other stage runs on OpenRouter or directly on Claude, chosen per role and per language.

## Setup

Needs `uv`, poppler (`pdfimages`, `pdftotext`) and ImageMagick (`magick`).

```sh
cp .env.example .env    # OPENROUTER_API_KEY for reading pages; CLAUDE_API_KEY for models named without a vendor prefix
uv sync
```

## A new book

```sh
uv run python -m nifty init mybook     # creates projects/mybook/project.toml, with every setting explained
# edit it and put the scans beside it
uv run python -m nifty run mybook --pages 10      # a trial, ending with an estimate for the whole book
uv run python -m nifty ocr mybook                 # read every page, then stop to look at the transcript
uv run python -m nifty glossary mybook            # write the glossaries, then stop to edit them
uv run python -m nifty run mybook                 # everything that remains
```

| Command | What it does |
|---|---|
| `init <name>` | Creates a project folder from `project.template.toml` |
| `run <project>` | All stages, resuming from whatever is saved. `--language`, `--pages N`, `--max-cost` |
| `ocr <project>` | Reads the pages, then stops |
| `glossary <project>` | Reads the pages and writes the glossaries, then stops |
| `status <project>` | How far each stage has got, for each language. Free, and safe to run during a run |
| `redo <project> <stage>` | Lists a stage's saved results; with `--yes`, deletes them and any later stage's, so the next run redoes them |
| `cost [project]` | Spend by model |
| `bakeoff <project> --pages 3,5 --models a,b` | Reads the same pages with several models, to compare |

`<project>` is a name under `projects/`, or any folder or `project.toml`. Progress appears as it goes, one line per finished page or section:

```
[russian translate] 12/51 (23%) c0012 · $3.10 so far · about 18m40s left · 4 already saved
```

## Settings

`project.template.toml` explains each one. The ones that matter most:

- **`sources`**: PDFs and images go through OCR; `.txt` and `.md` files are taken as already read. Page numbers follow this list, so only add to the end.
- **`about`, `source_notes`**: what the book is, and what a reader must not correct.
- **`[[translation]]`**: one block per language, run in the order listed.
- **`voice`**: how the translation should read. This is the setting that most changes the result: describe a tradition and give examples of phrasing and of what to avoid. `examples/johansen/project.toml` shows a full one.
- **`references`**: Wikipedia articles, other web pages and local text, Markdown or PDF files. These feed the glossary, so names and terms take their established forms.
- **`style_samples`**: texts whose voice the translator should echo. The start of each goes with every section, so keep them short.
- **`glossary_notes`**: decisions that overrule the references, such as "the diarist is Йохансен, not Юхансен".
- **`cross_check`**: an earlier translation in the project, which later languages use to confirm hard readings.
- **`script`, `length_ratio`, `refusal`**: tune the mechanical checks for the language. Set `length_ratio = []` for languages not written with spaces.
- **`[models]`**, **`[translation.models]`**: override any role in `defaults.toml`. `vendor/model` goes through OpenRouter; `claude-opus-5-5` goes directly to Anthropic.

## Results

In `output/<name>/`: `transcript.md`, and for each language `translation.md` and `translation.qa_report.md` (`trial.*` for a `--pages` run). `⟦p22⟧` marks where printed page 22 begins. The QA report lists pages to rescan, uncertain words, failed checks, and every proofreading edit and vocabulary change.

## Pipeline

1. **Extract.** Each page's original image, plus a contrast-normalised copy that removes colour casts and glare.
2. **Read.** Two models read each page separately, letter for letter, marking inferred words `[?word]` and lost text `[illegible]`. Typographic differences are ignored, differences only in `[?]` marks are settled in code, and anything else goes to a third model with the image.
3. **Glossary.** One call over the references and the whole book fixes names, terms and false friends. It is saved as `glossary.<language>.md` in the project folder and never overwritten, so edit it freely.
4. **Translate.** Pages are joined and cut at headings, or at paragraphs where there are none, so sentences crossing a page stay whole. A jump in printed page numbers forces a break and a visible note. Sections run in order, each seeing the end of the last. Output that fails the checks (refusals, lost markers, wrong length, text left in the source language or the wrong script) is retried with the failures named, then sent to the fallback model. If that fails too, the section appears as `[NOT TRANSLATED]`, and the next run tries it again.
5. **Proofread.** A model compares translation to source and lists exact find/replace edits, applied one at a time and logged. An edit that breaks a check is reverted alone. Advisory models only make suggestions, which go to the report.
6. **Align.** Sections cannot see each other's variants, so one call compares every distinct word of the translation with the glossary and returns whole-word replacements. To change a name everywhere, edit the glossary and run again; this is the only stage that repeats, for cents.

Finished work is kept in `work/<name>/`, by a fingerprint of the section's text, so a run can stop and resume, and a page added later redoes only the sections it touches. Unchanged calls come from `work/cache/` at no cost. Each command stops at `max_cost_usd`. Editing the glossary does not redo translated sections on its own; `redo <project> translate` does.

Sources and references are treated as material: the prompts tell the models that instructions inside them are not requests.

## Choosing models (trials of 2026-10-30, on a 1910 Norwegian diary)

Reading three hard pages, with disputed words checked against the photo:

| Model | Real errors | Cost/page | Notes |
|---|---|---|---|
| gemini-3.8-flash | 0 | $0.015 | First reader, at low reasoning ($0.0055); weaker on damaged pages at that setting |
| gemini-4-argon | 0 | $0.015 | Second reader; once silently corrected a printed error |
| gemini-3.5-flash | 0 | $0.043 | Accurate but expensive |
| claude-sonnet-5.5 | 1 | $0.040 | Too expensive for this role |
| gemini-3.1-flash-lite | 6 | $0.002 | Dropped a 15-word line; modernises spelling |
| gpt-5.6-terra | 11+ | $0.058 | Modernises spelling |
| gemini-3.5-flash-lite | 14+ | $0.003 | Dropped the same line |
| claude-haiku-5.5 | not scored | $0.002 | Modernises, and invented a word for a bleached one |

Settling disputes between readers: Sonnet, Argon and Haiku ruled identically on 14 real disputes, so Haiku is used.

Translation: Opus 5.5 reads best. Gemini 4 Argon costs about a third as much and twice let a Spanish word through, which proofreading caught. Gemini 3.8 Flash sometimes refuses, calling the text copyrighted, and writes a summary instead. Sonnet 5.5 left headings untranslated.

Run `bakeoff` on your own hardest pages before trusting this table for another language or typeface.

## Scanning

Glare bleaches words beyond recovery, and those show up in the QA report as `[?word]` or `[illegible]`. Avoid direct light and keep the page flat.

## Tests

`uv run --group dev pytest` runs everything with a scripted client, without a network or keys.
