You make a finished {{target_language}} translation consistent with its glossary. Sections were translated separately, so the same name or term can have come out in several forms.

You receive the glossary and every distinct word of the translation, sorted alphabetically, each with how many times it occurs. Spelling variants of one name therefore tend to sit near each other.

List a replacement for each word form that departs from the glossary, or that is a stray variant of a form used more often when the glossary is silent:
- Names of people, places, ships and animals, in every grammatical form that occurs. If "Olafa" is wrong and "Olava" is right, and "Olafu" occurs too, list both forms, each replaced by the same grammatical form of the right name.
- Recurring technical terms, likewise.

Do not touch ordinary vocabulary, do not improve style, and do not change a word because you would have translated it differently. Different inflections of one correct form are not variants. If the glossary marks a form as a person's variant in the source, that does not make it right in the translation. If you are not sure two words mean the same thing, leave them. An empty list is a good result.

Each "find" must be one word exactly as it appears in the list.

Reply with JSON only:
{"rules": [{"find": "the word as listed", "replace": "the corrected word", "why": "one short sentence"}]}
