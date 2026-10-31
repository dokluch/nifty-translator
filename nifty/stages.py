"""The pipeline: read the pages (two readers + adjudication), cut into sections, write the glossary,
translate, proofread, align the vocabulary, assemble."""
import difflib
import hashlib
import json
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import references
from .llm import ROOT, LLMError, image_part
from .progress import Progress, single
from .project import Project, ProjectError, Translation

MARKER = re.compile(r"⟦p\d+⟧")
WORD = re.compile(r"\w+", re.U)
LETTERS = re.compile(r"[^\W\d_]+", re.U)
# Checked only at the start of a reply: a real book can mention copyright further in.
REFUSAL = re.compile(r"unable to (provide|translate)|cannot (provide|translate)|can't (provide|translate)"
                     r"|copyrighted|here is a summary|broad summary", re.I)
PLACEHOLDER = re.compile(r"\[(?!\?|illegible\]|TN:)")
MAX_EDIT_WORDS = 80
MAX_SOURCE_SHARE = 0.6  # of the longer words in a translation that are also in its source: text left untranslated


def prompt(name: str, **values: str) -> str:
    text = (ROOT / "prompts" / f"{name}.md").read_text()
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    if unfilled := re.search(r"\{\{\w+\}\}", text):
        raise ProjectError(f"prompts/{name}.md: nothing supplied for {unfilled.group(0)}")
    return text


def values(project: Project, translation: Translation | None = None) -> dict:
    found = {"source_language": project.source_language, "about": project.about or "(not described)",
             "source_notes": project.source_notes}
    if translation:
        found |= {"target_language": translation.language, "voice": translation.voice or "Natural and plain.",
                  "glossary_notes": translation.glossary_notes or "(none)"}
    return found


def page_images(pages_dir: Path, pid: str) -> list[dict]:
    return [image_part(pages_dir / f"{pid}.jpg"), image_part(pages_dir / f"{pid}.enh.jpg")]


def parse_page(raw: str) -> dict:
    head, sep, body = raw.partition("\n---\n")
    if not sep:
        raise LLMError(f"transcription lacks the header separator: {raw[:120]!r}")
    printed = re.search(r"PRINTED_PAGE:\s*(.+)", head)
    condition = re.search(r"CONDITION:\s*(.+)", head)
    return {
        "printed": printed.group(1).strip() if printed else "none",
        "condition": condition.group(1).strip() if condition else "unknown",
        "text": body.strip(),
    }


SUPERSCRIPTS = str.maketrans("ᵃᵇᵈᵉᵗⁿˢªº", "abdetnsa°")


def canonical(text: str) -> str:
    """One spelling for what the models render differently: 2ᵈᵉⁿ, 2^{den} and 2den are all 2den."""
    return re.sub(r"\^\{(\w+)\}", r"\1", text).translate(SUPERSCRIPTS)


def typographic(text: str) -> list[str]:
    """Words with dash and quote style folded: those differences are not worth adjudicating."""
    return canonical(text).translate(str.maketrans("–—”“„’´", '--"""\'\'')).split()


def word_diffs(a: str, b: str, context: int = 3) -> list[str]:
    wa, wb = typographic(a), typographic(b)
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if tag != "equal":
            before = " ".join(wa[max(0, i1 - context):i1])
            out.append(f'...{before} | A: "{" ".join(wa[i1:i2])}" | B: "{" ".join(wb[j1:j2])}"')
    return out


def merge_uncertainty(a: str, b: str) -> str | None:
    """If the readers differ only in [?word] marks, keeps A with every mark either one made.
    Returns None if they differ in any other way, which needs a look at the image."""
    single = re.compile(r"\[\?(\S*?)\]")
    if typographic(single.sub(r"\1", a)) != typographic(single.sub(r"\1", b)):
        return None
    if re.search(r"\[\?[^\]]*\s", a + b):
        return None
    parts, words_b = re.split(r"(\s+)", a), b.split()
    for i in range(0, len(parts), 2):
        if parts[i] and "[?" not in parts[i] and "[?" in words_b[i // 2]:
            parts[i] = words_b[i // 2]
    return "".join(parts)


def transcribe_saved(project: Project, pid: str) -> dict | None:
    if (text_page := project.work / "pages" / f"{pid}.txt").exists():
        return {"pid": pid, "printed": "none", "condition": "good", "text": text_page.read_text().strip()}
    out = project.work / "transcripts" / f"{pid}.json"
    return json.loads(out.read_text()) if out.exists() else None


def transcribe_page(client, project: Project, pid: str) -> dict:
    pages_dir, out_dir, models = project.work / "pages", project.work / "transcripts", project.models
    if saved := transcribe_saved(project, pid):
        return saved
    out = out_dir / f"{pid}.json"

    images = page_images(pages_dir, pid)
    content = [{"type": "text", "text": "Transcribe this page."}, *images]
    fb = models["ocr_fallback"]
    system = prompt("transcribe", **values(project))
    a = client.chat_with_fallback([models["ocr_a"], fb], system, content, label=f"{pid}:reader_a",
                                  reasoning=models.get("ocr_a_reasoning"))
    b = client.chat_with_fallback([models["ocr_b"], fb], system, content, label=f"{pid}:reader_b")
    pa, pb = parse_page(a["text"]), parse_page(b["text"])
    diffs = word_diffs(pa["text"], pb["text"])

    final = pa
    unsupported = []
    merged = merge_uncertainty(pa["text"], pb["text"]) if diffs else None
    if merged is not None:
        final = {**pa, "text": merged}
        if pa["condition"].startswith("good"):
            final["condition"] = pb["condition"]
    elif diffs:
        text = (f"TRANSCRIPTION A:\n{a['text']}\n\nTRANSCRIPTION B:\n{b['text']}\n\n"
                "DIFFERENCES:\n" + "\n".join(diffs))
        # The normalised image only: the original doubles the cost on vendors that bill by pixel.
        r = client.chat_with_fallback([models["ocr_adjudicate"], fb], prompt("adjudicate", **values(project)),
                                      [{"type": "text", "text": text}, images[1]], label=f"{pid}:adjudicate")
        final = parse_page(r["text"])
        # Words neither reader saw mean the adjudicator normalised or invented something.
        seen = {w.lower() for w in WORD.findall(pa["text"] + " " + pb["text"])} | {"illegible"}
        unsupported = sorted({w for w in WORD.findall(final["text"]) if w.lower() not in seen})

    result = {**final, "pid": pid, "reader_a": pa["text"], "reader_b": pb["text"],
              "diffs": diffs, "unsupported_words": unsupported}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def transcribe_all(client, project: Project, ids: list[str]) -> list[dict]:
    (project.work / "transcripts").mkdir(parents=True, exist_ok=True)
    progress = Progress("read pages", len(ids), client)

    def one(pid):
        with progress.item(pid):
            return transcribe_page(client, project, pid)

    with ThreadPoolExecutor(project.run["concurrency"]) as pool:
        return list(pool.map(one, ids))


def book_order(pages: list[dict]) -> list[dict]:
    """Sorts by printed page number, so a page scanned late still lands in its place.
    Unnumbered pages stay directly after whatever preceded them in the scan."""
    keyed, key = [], 0
    for page in pages:
        key = int(page["printed"]) if page["printed"].isdigit() else key
        keyed.append((key, page))
    return [page for _, page in sorted(keyed, key=lambda pair: pair[0])]


def text_key(text: str) -> str:
    """Names a section's saved results by its text: section numbers shift when pages are added."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def flush_chunk(chunks: list, buf: list, gaps: dict, target_words: int) -> list:
    """Closes a chunk. A short tail is better translated with what precedes it, unless a gap divides them."""
    if buf:
        if chunks and len(chunks) not in gaps and len(" ".join(buf).split()) < target_words / 3:
            chunks[-1].extend(buf)
        else:
            chunks.append(buf)
    return []


def split_long(block: list[str], limit: int) -> list[list[str]]:
    """Text without headings has nowhere to cut but its paragraphs. Diary entries stay whole."""
    if block[0] == "GAP" or len(" ".join(block).split()) <= limit:
        return [block]
    parts, current, words = [], [], 0
    for line in block:
        if not line.strip() and words >= limit / 3:
            parts.append(current)
            current, words = [], 0
        current.append(line)
        words += len(line.split())
    return [*parts, current] if current else parts


def build_chunks(pages: list[dict], target_words: int) -> list[dict]:
    """Joins pages into running text with page markers, then cuts at headings so that
    sentences and diary entries crossing a page break are translated whole."""
    blocks, current = [], []
    previous = None
    for page in pages:
        # Printed numbers that jump mean pages are missing from the scan, and the text
        # on either side does not connect, so it must not be translated as one passage.
        if page["printed"].isdigit():
            number = int(page["printed"])
            if previous and number > previous + 1:
                if current:
                    blocks.append(current)
                    current = []
                missing = str(previous + 1) if number == previous + 2 else f"{previous + 1}-{number - 1}"
                blocks.append(["GAP", missing])
            previous = number
        lines = canonical(page["text"]).split("\n")
        tag = f"⟦p{page['printed']}⟧" if page["printed"].isdigit() else f"⟦{page['pid']}⟧"
        pending = tag
        for line in lines:
            if line.startswith("## ") and current:
                blocks.append(current)
                current = []
            if pending and line.strip():
                # A marker never goes before "## ", or the heading would stop being one.
                line = f"{pending}\n{line}" if line.startswith("## ") else f"{pending} {line}"
                pending = ""
            current.append(line)
        if pending:
            current.append(pending)
    if current:
        blocks.append(current)

    chunks, buf, gaps = [], [], {}
    for block in (part for block in blocks for part in split_long(block, target_words * 3)):
        if block[0] == "GAP":
            buf = flush_chunk(chunks, buf, gaps, target_words)
            gaps[len(chunks)] = block[1]
            continue
        buf.extend(block)
        if len(" ".join(buf).split()) >= target_words:
            chunks.append(buf)
            buf = []
    flush_chunk(chunks, buf, gaps, target_words)

    result = []
    for i, c in enumerate(chunks, 1):
        text = "\n".join(c).strip()
        result.append({"id": f"c{i:04d}", "source": text, "markers": MARKER.findall(text),
                       "gap_before": gaps.get(i - 1)})
    return result


def write_glossary(client, project: Project, translation: Translation, chunks: list[dict], trial: bool) -> str:
    """A glossary already in the project folder is never overwritten: it is there to be edited.
    One written during a trial stays out of it, since it has seen only the trial's pages."""
    path = project.dir / f"glossary.{translation.slug}.md"
    if trial and not path.exists():
        path = project.work / translation.slug / "glossary.trial.md"
    if path.exists():
        return path.read_text()
    limit = project.run["reference_chars"]
    found = references.load([*project.references, *translation.references], project.dir, project.work / "references")
    material = "\n\n".join(f'<reference name="{label}">\n{text[:limit]}\n</reference>' for label, text in found)
    book = "\n\n".join(MARKER.sub("", c["source"]) for c in chunks)
    with single(f"{translation.slug} glossary", f"reading {len(found)} references and the book", client):
        r = client.chat(translation.models["glossary"], prompt("glossary", **values(project, translation)),
                        f"{material or '(no references)'}\n\n<book>\n{book}\n</book>",
                        label=f"{translation.slug}:glossary", max_tokens=64000)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(r["text"] + "\n")
    print(f"glossary written: {path}")
    return path.read_text()


def check_translation(source: str, text: str, translation: Translation) -> list[str]:
    """Mechanical checks: anything found here is a defect, whatever the model believes."""
    problems = []
    if match := REFUSAL.search(text[:300]):
        problems.append(f"refusal or chatbot text: {match.group(0)!r}")
    if translation.refusal and (match := re.search(translation.refusal, text[:300], re.I)):
        problems.append(f"refusal or chatbot text: {match.group(0)!r}")
    plain = MARKER.sub("", text).replace("[illegible]", "").replace("[TN:", "")
    if translation.script:
        foreign = [w for w in LETTERS.findall(plain) if len(w) > 1 and w not in translation.allow_foreign
                   and not unicodedata.name(w[0], "").startswith(translation.script.upper())]
        if len(foreign) > translation.max_foreign:
            problems.append(f"{len(foreign)} words outside {translation.script}: {foreign[:12]}")
    in_source = {w.lower() for w in LETTERS.findall(source)}
    long_words = [w.lower() for w in LETTERS.findall(plain) if len(w) >= 4]
    if len(long_words) > 60 and sum(w in in_source for w in long_words) / len(long_words) > MAX_SOURCE_SHARE:
        problems.append("most of the text is the same as the source: possibly left untranslated")
    if MARKER.findall(source) != MARKER.findall(text):
        problems.append(f"page markers differ: {MARKER.findall(source)} vs {MARKER.findall(text)}")
    if source.count("[illegible]") != text.count("[illegible]"):
        problems.append(f"[illegible]: {source.count('[illegible]')} in source, {text.count('[illegible]')} in translation")
    if source.count("[?") != text.count("[?"):
        problems.append(f"[?] marks: {source.count('[?')} in source, {text.count('[?')} in translation")
    if sum(l.startswith("## ") for l in source.split("\n")) != sum(l.startswith("## ") for l in text.split("\n")):
        problems.append("heading count differs")
    ns, nt = len(source.split()), len(text.split())
    if translation.length_ratio and ns > 60 and not translation.length_ratio[0] <= nt / ns <= translation.length_ratio[1]:
        problems.append(f"length ratio {nt / ns:.2f} ({ns} words in, {nt} out): possible omission or padding")
    return problems


def translate_section(client, translation: Translation, system: str, chunk: dict, request: str) -> dict:
    """Output that fails the checks is never kept. Each model gets one corrective retry;
    if all fail the section stays empty and the assembled book says so where it belongs."""
    models, problems = translation.models, []
    for model in (models["translate"], models["translate_fallback"]):
        for attempt in ("", "_retry"):
            message = request
            if attempt:
                message += "\n\nA previous attempt failed these checks; fix them:\n" + "\n".join(problems)
            try:
                r = client.chat(model, system, message, label=f"{chunk['id']}:{translation.slug}{attempt}")
            except LLMError as e:
                print(f"  ! {e}")
                problems = [str(e)]
                break
            problems = check_translation(chunk["source"], r["text"], translation)
            if not problems:
                return {**chunk, "text": r["text"], "model": model, "problems": []}
            print(f"  {chunk['id']} {model}{attempt} failed: {problems}")
    return {**chunk, "text": "", "model": None, "problems": ["NOT TRANSLATED", *problems]}


def cross_check_text(project: Project, translation: Translation, chunk: dict) -> str:
    if not translation.cross_check:
        return ""
    slug = project.translation(translation.cross_check).slug
    saved = project.work / slug / "final" / f"{text_key(chunk['source'])}.json"
    return json.loads(saved.read_text())["text"] if saved.exists() else ""


def translate_sections(client, project: Project, translation: Translation, chunks: list[dict], glossary: str) -> list[dict]:
    """Sequential on purpose: each section sees the end of the previous one, for continuity."""
    out_dir = project.work / translation.slug / "translate"
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = project.run["style_chars"]
    samples = references.load(translation.style_samples, project.dir, project.work / "references")
    style = "".join(f"\n\nA sample of the voice to have in your ear; do not quote it:\n<sample>\n{text[:limit]}\n</sample>"
                    for _, text in samples)
    system = f"{prompt('translate', **values(project, translation))}{style}\n\n{glossary}"
    previous, results = "", []
    progress = Progress(f"{translation.slug} translate", len(chunks), client)
    for chunk in chunks:
        out = out_dir / f"{text_key(chunk['source'])}.json"
        result = json.loads(out.read_text()) if out.exists() else None
        with progress.item(chunk["id"]):
            if not result:
                if chunk.get("gap_before"):
                    previous = ""
                context = f"End of the previous section, for continuity only; do not translate it again:\n{previous}\n\n" if previous else ""
                if reference := MARKER.sub("", cross_check_text(project, translation, chunk)):
                    context += f"{translation.cross_check} translation, for reference only:\n{reference}\n\n"
                result = translate_section(client, translation, system, chunk, f"{context}Translate this section:\n\n{chunk['source']}")
                # A failure is not saved, so the next run tries that section again.
                if result["text"]:
                    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        if result["text"]:
            previous = " ".join(MARKER.sub("", result["text"]).split()[-150:])
        results.append({**result, "id": chunk["id"], "gap_before": chunk.get("gap_before")})
    return results


def parse_json(text: str) -> dict:
    # raw_decode tolerates anything a model writes after its JSON, and this skips what it writes before.
    return json.JSONDecoder().raw_decode(text[text.index("{"):])[0]


def apply_edits(text: str, issues: list[dict]) -> str:
    for issue in issues:
        find, replace = issue.get("find") or "", issue.get("replace") or ""
        if issue.get("type") == "transcription" or not find:
            issue["status"] = "for_human"
        elif text.count(find) != 1:
            issue["status"] = "not_applied: text not found exactly once"
        elif PLACEHOLDER.search(replace) or len(replace.split()) > MAX_EDIT_WORDS:
            issue["status"] = "not_applied: placeholder or oversized rewrite"
        elif MARKER.findall(find) != MARKER.findall(replace):
            issue["status"] = "not_applied: would alter a page marker"
        else:
            text = text.replace(find, replace)
            issue["status"] = "applied"
    return text


def proofread_section(client, project: Project, translation: Translation, chunk: dict, system: str, glossary: str) -> dict:
    """Each proofreader lists exact find/replace edits instead of returning a rewrite,
    so every change is on record and it cannot silently alter what it did not flag.
    They run in turn, the later ones checking the text as already corrected."""
    if not chunk["text"]:
        return {**chunk, "issues": []}
    out_dir = project.work / translation.slug / "proofread"
    out = out_dir / f"{text_key(chunk['source'])}.json"
    if out.exists():
        done = json.loads(out.read_text())
        if done["draft"] == chunk["text"]:
            return {**done, "id": chunk["id"], "gap_before": chunk.get("gap_before")}
    models = translation.models
    text, all_issues, problems = chunk["text"], [], []
    advisory = models["proofread_advisory"]
    reference = cross_check_text(project, translation, chunk)
    cross = f"{translation.cross_check.upper()} REFERENCE:\n{reference}\n\n" if reference else ""
    for model in [*models["proofread"], *advisory]:
        message = f"SOURCE TEXT:\n{chunk['source']}\n\n{cross}TRANSLATION:\n{text}"
        try:
            # The glossary goes in the system prompt, where it is cached between sections.
            r = client.chat_with_fallback([model, models["fallback"]], f"{system}\n\n{glossary}", message,
                                          label=f"{chunk['id']}:proofread", json_mode=True, effort="medium")
            issues = parse_json(r["text"]).get("issues", [])
        except (LLMError, ValueError, AttributeError) as e:
            problems.append(f"{model} proofreading failed, its edits skipped; run again to retry: {e}")
            continue
        for issue in issues:
            issue["by"] = model
        # Advisory models only suggest: a second vendor's opinions are not safe to apply unseen.
        if model in advisory:
            for issue in issues:
                issue["status"] = "suggested"
            all_issues += issues
            continue
        for issue in issues:
            # One at a time, so an edit that breaks a check does not take the good ones with it.
            candidate = apply_edits(text, [issue])
            broken = check_translation(chunk["source"], candidate, translation)
            if broken and issue["status"] == "applied":
                issue["status"] = f"reverted: {broken[0]}"
            else:
                text = candidate
        all_issues += issues
    result = {**chunk, "draft": chunk["text"], "text": text, "issues": all_issues, "problems": [*chunk["problems"], *problems]}
    if not problems:
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def proofread_sections(client, project: Project, translation: Translation, chunks: list[dict], glossary: str) -> list[dict]:
    (project.work / translation.slug / "proofread").mkdir(parents=True, exist_ok=True)
    system = prompt("proofread", **values(project, translation))
    progress = Progress(f"{translation.slug} proofread", len(chunks), client)

    def one(chunk):
        with progress.item(chunk["id"]):
            return proofread_section(client, project, translation, chunk, system, glossary)

    with ThreadPoolExecutor(project.run["concurrency"]) as pool:
        return list(pool.map(one, chunks))


def vocabulary(chunks: list[dict]) -> Counter:
    return Counter(w for c in chunks for w in LETTERS.findall(MARKER.sub("", c["text"])) if len(w) > 2)


def align_rules(client, translation: Translation, chunks: list[dict], glossary: str) -> list[dict]:
    """One call over the whole book's vocabulary, since no section can see another's variants.
    Rules replace whole words, so they cannot reach a marker or part of a longer word."""
    words = vocabulary(chunks)
    listing = "\n".join(f"{word} {count}" for word, count in sorted(words.items(), key=lambda kv: kv[0].lower()))
    system = f"{prompt('align', target_language=translation.language)}"
    r = client.chat(translation.models["align"], system, f"GLOSSARY:\n{glossary}\n\nWORDS:\n{listing}",
                    label=f"{translation.slug}:align", json_mode=True, effort="medium", max_tokens=32000)
    rules = []
    for rule in parse_json(r["text"]).get("rules", []):
        find, replace = rule.get("find") or "", rule.get("replace") or ""
        if find in words and replace and replace != find and not PLACEHOLDER.search(replace) and "⟦" not in replace:
            rules.append({**rule, "count": words[find], "status": "applied"})
        else:
            rules.append({**rule, "status": "not_applied: not a word of the text"})
    return rules


def apply_rules(text: str, rules: list[dict]) -> str:
    table = {r["find"]: r["replace"] for r in rules if r["status"] == "applied"}
    if not table:
        return text
    words = "|".join(re.escape(w) for w in sorted(table, key=len, reverse=True))
    return re.sub(rf"(?<!\w)({words})(?!\w)", lambda m: table[m.group(1)], text)


def align_sections(client, project: Project, translation: Translation, chunks: list[dict], glossary: str) -> tuple[list[dict], list[dict]]:
    """A section the rules would break keeps its proofread text, and the report says so."""
    done = [c for c in chunks if c["text"]]
    try:
        with single(f"{translation.slug} align", "checking the whole vocabulary against the glossary", client):
            rules = align_rules(client, translation, done, glossary) if done else []
    except (ValueError, AttributeError) as e:
        print(f"  ! alignment reply unreadable, no rules applied: {e}")
        return [{**c, "problems": [*c["problems"], f"alignment skipped: {e}"]} for c in chunks], []
    result = []
    for chunk in chunks:
        candidate = apply_rules(chunk["text"], rules)
        if chunk["text"] and (broken := check_translation(chunk["source"], candidate, translation)):
            result.append({**chunk, "problems": [*chunk["problems"], f"alignment skipped here: {broken[0]}"]})
        else:
            result.append({**chunk, "text": candidate})
    return result, rules


def assemble(project: Project, translation: Translation, pages: list[dict], chunks: list[dict], rules: list[dict], trial: bool) -> Path:
    out_dir = project.output / translation.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    final = project.work / translation.slug / "final"
    final.mkdir(parents=True, exist_ok=True)
    for c in chunks:
        if c["text"]:
            (final / f"{text_key(c['source'])}.json").write_text(json.dumps({"text": c["text"]}, ensure_ascii=False))

    def gap_note(c):
        return f"[Printed p. {c['gap_before']} is not in this scan]\n\n" if c.get("gap_before") else ""
    stem = "trial" if trial else "translation"
    path = out_dir / f"{stem}.md"
    path.write_text("\n\n".join(gap_note(c) + (c["text"] or f"[NOT TRANSLATED: section {c['id']}, see the QA report]")
                                for c in chunks) + "\n")

    lines = ["# QA report", "", "## Printed pages missing from the scan", ""]
    lines += [f"- p. {c['gap_before']}" for c in chunks if c.get("gap_before")] or ["None."]
    lines += [""]
    degraded = [p for p in pages if not p["condition"].startswith("good")]
    lines += ["## Pages with damaged or hidden text (rescan these)", ""]
    lines += [f"- {p['pid']} (printed {p['printed']}): {p['condition']}" for p in degraded] or ["None."]
    uncertain = [(p, re.findall(r"\[\?[^\]]+\]|\[illegible\]", p["text"])) for p in pages]
    lines += ["", "## Uncertain readings", ""]
    lines += [f"- {p['pid']} (printed {p['printed']}): {' '.join(u)}" for p, u in uncertain if u] or ["None."]
    lines += ["", "## Words the adjudicator introduced that neither reader read", ""]
    lines += [f"- {p['pid']}: {', '.join(p['unsupported_words'])}" for p in pages if p.get("unsupported_words")] or ["None."]
    lines += ["", "## Failed mechanical checks", ""]
    lines += [f"- {c['id']}: {x}" for c in chunks for x in c.get("problems", [])] or ["None."]
    lines += ["", "## Vocabulary aligned with the glossary", ""]
    lines += [f"- {r['status']}: \"{r.get('find', '')}\" → \"{r.get('replace', '')}\" ×{r.get('count', 0)} ({r.get('why', '')})" for r in rules] or ["None."]
    lines += ["", "## Proofreading", ""]
    for c in chunks:
        for i in c.get("issues", []):
            lines.append(f"- {c['id']} [{i.get('severity')} {i.get('type')}] {i.get('status')}: "
                         f"\"{i.get('source', '')}\": \"{i.get('find', '')}\" → \"{i.get('replace', '')}\" ({i.get('why', '')})")
    (out_dir / f"{stem}.qa_report.md").write_text("\n".join(lines) + "\n")
    return path
