"""Usage: uv run python -m nifty init mybook                  a new project folder with commented settings
          uv run python -m nifty run mybook [--language Russian] [--pages 10]
          uv run python -m nifty ocr mybook                   read the pages, then stop
          uv run python -m nifty glossary mybook              write the glossaries to review, then stop
          uv run python -m nifty status mybook                how far each stage has got, at no cost
          uv run python -m nifty redo mybook proofread --language Russian --yes
          uv run python -m nifty bakeoff mybook --pages 3,5,8 --models a,b,c
          uv run python -m nifty cost [mybook]"""
import argparse
import json
import shutil
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import pages as page_files, references, stages
from .llm import LEDGER, ROOT, BudgetExceeded, Client, LLMError
from .project import ProjectError, load, resolve


def run(args) -> None:
    project = load(args.project)
    client = Client(args.max_cost or project.run["max_cost_usd"], project.name)
    chosen = [project.translation(args.language)] if args.language else project.translations
    # A reference that cannot be read should stop the run before it has cost anything.
    for translation in chosen:
        if args.stop_after != "ocr":
            references.load([*project.references, *translation.references, *translation.style_samples], project.dir, project.work / "references")
    ids = page_files.extract_book(project.source_files(), project.work / "pages")
    total = len(ids)
    ids = ids[:args.pages]
    trial = len(ids) < total
    pages = stages.book_order(stages.transcribe_all(client, project, ids))
    chunks = stages.build_chunks(pages, project.run["chunk_target_words"])
    project.output.mkdir(parents=True, exist_ok=True)
    (project.output / ("trial.transcript.md" if trial else "transcript.md")).write_text("\n\n".join(c["source"] for c in chunks) + "\n")
    print(f"{len(ids)} of {total} pages make {len(chunks)} sections; {project.output}/")
    if args.stop_after == "ocr":
        print(f"done; ${client.spent:.3f} spent this run")
        return

    for translation in chosen:
        if translation.cross_check and not any(stages.cross_check_text(project, translation, c) for c in chunks):
            raise ProjectError(f"{translation.language} needs the {translation.cross_check} translation first")
        glossary = stages.write_glossary(client, project, translation, chunks, trial)
        if args.stop_after == "glossary":
            continue
        drafts = stages.translate_sections(client, project, translation, chunks, glossary)
        checked = stages.proofread_sections(client, project, translation, drafts, glossary)
        final, rules = stages.align_sections(client, project, translation, checked, glossary)
        path = stages.assemble(project, translation, pages, final, rules, trial)
        print(f"{translation.language}: {path}; ${client.spent:.3f} so far")
    print(f"done; ${client.spent:.3f} spent this run")
    if trial and (estimate := whole_book_estimate(project.name, len(ids), total)):
        print(f"at this rate the whole book, {total} pages, would cost about ${estimate:.2f}")


def init(args) -> None:
    folder = ROOT / "projects" / args.name
    if folder.exists():
        raise ProjectError(f"{folder} already exists")
    folder.mkdir(parents=True)
    (folder / "project.toml").write_text((ROOT / "project.template.toml").read_text().replace("NAME", args.name))
    print(f"{folder}/project.toml: fill in the settings, put the scans beside it, then:\n"
          f"  uv run python -m nifty run {args.name} --pages 10")


def status(args) -> None:
    """Reads only what is on disk, so it is free and safe to run during a run."""
    project = load(args.project)
    work = project.work
    sources = project.source_files()
    pages = sorted(p for p in (work / "pages").glob("*") if p.suffix == ".txt" or (p.suffix == ".jpg" and ".enh" not in p.name))
    ids = [p.stem for p in pages]
    read = [i for i in ids if (work / "pages" / f"{i}.txt").exists() or (work / "transcripts" / f"{i}.json").exists()]
    print(f"{project.name}: {len(sources)} sources, {project.source_language}")
    print(f"  pages extracted {len(ids)}, read {len(read)}")
    chunks = []
    if read and len(read) == len(ids):
        loaded = [stages.transcribe_saved(project, i) for i in ids]
        chunks = stages.build_chunks(stages.book_order(loaded), project.run["chunk_target_words"])
        print(f"  sections {len(chunks)}")
    for t in project.translations:
        glossary = "written" if (project.dir / f"glossary.{t.slug}.md").exists() else "not yet"
        counts = []
        for stage in ("translate", "proofread", "final"):
            saved = {p.stem for p in (work / t.slug / stage).glob("*.json")}
            counts.append(f"{stage} {sum(stages.text_key(c['source']) in saved for c in chunks)}/{len(chunks)}" if chunks else f"{stage} {len(saved)}")
        print(f"  {t.language}: glossary {glossary}; " + ", ".join(counts))
        report = project.output / t.slug / "translation.qa_report.md"
        if report.exists():
            print(f"    {report}")
    print(f"  spent so far ${ledger_totals(project.name)[1]:.2f}")


def redo(args) -> None:
    """Paid results are thrown away only when asked twice over."""
    project = load(args.project)
    chosen = [project.translation(args.language)] if args.language else project.translations
    targets = []
    for t in chosen:
        if args.stage == "glossary":
            targets.append(project.dir / f"glossary.{t.slug}.md")
        else:
            # Later stages are built on this one, so they go with it.
            order = ["translate", "proofread"]
            targets += [project.work / t.slug / stage for stage in order[order.index(args.stage):]]
    existing = [p for p in targets if p.exists()]
    for path in existing:
        count = len(list(path.glob("*.json"))) if path.is_dir() else 1
        print(f"{path}: {count} saved")
    if not existing:
        print("nothing saved for that")
    elif not args.yes:
        print("nothing deleted; add --yes to delete these and pay to redo them on the next run")
    else:
        for path in existing:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        print("deleted")


def bakeoff(args) -> None:
    """Reads the same pages with each model and saves them for side-by-side comparison."""
    project = load(args.project)
    client = Client(project.run["max_cost_usd"], project.name)
    ids = page_files.extract_book(project.source_files(), project.work / "pages")
    chosen = [ids[int(n) - 1] for n in args.pages.split(",")]
    out_dir = project.work / "bakeoff"
    out_dir.mkdir(parents=True, exist_ok=True)
    system = stages.prompt("transcribe", **stages.values(project))

    def one(job):
        model, pid = job
        content = [{"type": "text", "text": "Transcribe this page."}, *stages.page_images(project.work / "pages", pid)]
        try:
            r = client.chat(model, system, content, label=f"{pid}:bakeoff")
        except LLMError as e:
            print(f"  ! {e}")
            return
        (out_dir / f"{pid}__{model.replace('/', '_')}.txt").write_text(r["text"])
        print(f"{model:36} {pid} ${r['cost']:.4f} {len(r['text'])} chars")

    jobs = [(m, p) for m in args.models.split(",") for p in chosen]
    with ThreadPoolExecutor(project.run["concurrency"]) as pool:
        list(pool.map(one, jobs))
    print(f"${client.spent:.3f} spent")


def ledger_totals(name: str | None = None) -> tuple[dict, float]:
    totals = defaultdict(float)
    for line in LEDGER.read_text().splitlines() if LEDGER.exists() else []:
        row = json.loads(line)
        if name is None or row.get("project") == name:
            totals[row["model"]] += row["cost"]
    return totals, sum(totals.values())


def whole_book_estimate(name: str, pages: int, total: int) -> float:
    """From the ledger, so a resumed trial still counts what it paid for earlier. The glossary and
    the alignment are one call each however long the book, so they are not multiplied up."""
    per_page = once = 0.0
    for line in LEDGER.read_text().splitlines() if LEDGER.exists() else []:
        row = json.loads(line)
        if row.get("project") == name:
            if row["label"].endswith((":glossary", ":align")):
                once += row["cost"]
            else:
                per_page += row["cost"]
    return once + per_page / pages * total


def cost(args) -> None:
    totals, total = ledger_totals(load(args.project).name if args.project else None)
    for model, c in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"{model:40} ${c:.4f}")
    print(f"{'total':40} ${total:.4f}")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # so progress lines arrive in order when piped to a file
    parser = argparse.ArgumentParser(prog="nifty")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create a project folder")
    p.add_argument("name")
    p.set_defaults(func=init)
    for command, stop, about in (("run", None, "all stages, resuming from what is saved"),
                                 ("ocr", "ocr", "read the pages, then stop"),
                                 ("glossary", "glossary", "write the glossaries to review, then stop")):
        p = sub.add_parser(command, help=about)
        p.add_argument("project", help="a project's name, its folder, or its project.toml")
        p.add_argument("--language", help="only this translation; by default all, in the order listed")
        p.add_argument("--pages", type=int, help="only the first N pages, as a trial, with an estimate for the whole book")
        p.add_argument("--max-cost", type=float)
        p.set_defaults(func=run, stop_after=stop)
    p = sub.add_parser("status", help="how far each stage has got, at no cost")
    p.add_argument("project")
    p.set_defaults(func=status)
    p = sub.add_parser("redo", help="delete a stage's saved results so the next run does them again")
    p.add_argument("project")
    p.add_argument("stage", choices=["glossary", "translate", "proofread"])
    p.add_argument("--language")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=redo)
    p = sub.add_parser("bakeoff", help="read the same pages with several models, to compare")
    p.add_argument("project")
    p.add_argument("--pages", required=True, help="1-based page numbers, comma separated")
    p.add_argument("--models", required=True, help="OpenRouter model ids, comma separated")
    p.set_defaults(func=bakeoff)
    p = sub.add_parser("cost", help="spend by model, for one project or all")
    p.add_argument("project", nargs="?")
    p.set_defaults(func=cost)
    args = parser.parse_args()
    try:
        args.func(args)
    except BudgetExceeded as e:
        raise SystemExit(f"Stopped, finished work is saved; run again to resume. {e}")
    except (LLMError, ProjectError) as e:
        raise SystemExit(f"Stopped, finished work is saved. {e}")


if __name__ == "__main__":
    main()
