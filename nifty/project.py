"""A project is one book: its sources, its languages and what the translators may consult."""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .llm import OUTPUT, ROOT, WORK
from .pages import SOURCES

OCR_ROLES = ("ocr_a", "ocr_b", "ocr_adjudicate", "ocr_fallback")


class ProjectError(RuntimeError):
    pass


@dataclass
class Translation:
    language: str
    models: dict
    voice: str = ""  # how it should read: the voice, the typography, the conventions of the language
    references: list[str] = field(default_factory=list)  # Wikipedia articles, other URLs and files, for the glossary
    style_samples: list[str] = field(default_factory=list)  # texts whose voice the translator should have in its ear
    glossary_notes: str = ""  # decisions the glossary must follow, whatever the references say
    cross_check: str = ""  # an earlier translation in this project, for confirming hard readings
    script: str = ""  # e.g. "cyrillic": words in other scripts count as text left untranslated
    allow_foreign: list[str] = field(default_factory=list)  # foreign words the source really quotes
    max_foreign: int = 8
    length_ratio: list[float] = field(default_factory=lambda: [0.8, 1.45])  # [] for languages not split by spaces
    refusal: str = ""  # a regular expression for how a model refuses in this language

    @property
    def slug(self) -> str:
        return self.language.lower().replace(" ", "-")


@dataclass
class Project:
    name: str
    dir: Path
    sources: list[str]
    source_language: str
    translations: list[Translation]
    models: dict
    run: dict
    about: str = ""  # what the book is: who wrote it, when, for whom
    source_notes: str = ""  # what a reader must not correct, such as old spelling
    references: list[str] = field(default_factory=list)  # consulted for every language

    @property
    def work(self) -> Path:
        return WORK / self.name

    @property
    def output(self) -> Path:
        return OUTPUT / self.name

    def translation(self, language: str) -> Translation:
        for t in self.translations:
            if t.language.lower() == language.lower():
                return t
        raise ProjectError(f"no [[translation]] for {language!r}; the project has {[t.language for t in self.translations]}")

    def source_files(self) -> list[Path]:
        """In the order listed, since page numbers follow it. Patterns and folders are sorted by name."""
        found = []
        for entry in self.sources:
            path = self.dir / entry
            if path.is_dir():
                matches = sorted(p for p in path.rglob("*") if p.suffix.lower() in SOURCES)
            elif path.exists():
                matches = [path]
            else:
                matches = sorted(p for p in self.dir.glob(entry) if p.suffix.lower() in SOURCES)
            if not matches:
                raise ProjectError(f"sources: nothing found for {entry!r} in {self.dir}")
            found += matches
        for path in found:
            if path.suffix.lower() not in SOURCES:
                raise ProjectError(f"sources: cannot read {path.suffix} files: {path}")
        return found


def resolve(path: str | Path) -> Path:
    """A bare name means a folder under projects/, so commands stay short."""
    named = ROOT / "projects" / str(path)
    return named if not Path(path).exists() and named.exists() else Path(path).resolve()


def load(path: str | Path) -> Project:
    path = resolve(path)
    if path.is_dir():
        path = path / "project.toml"
    if not path.exists():
        raise ProjectError(f"no project file at {path}")
    data = tomllib.loads(path.read_text())
    defaults = tomllib.loads((ROOT / "defaults.toml").read_text())
    for key in ("name", "sources", "source_language", "translation"):
        if key not in data:
            raise ProjectError(f"{path.name} needs {key!r}")
    models = {**defaults["models"], **data.pop("models", {})}
    settings = {**defaults["run"], **data.pop("run", {})}
    for role in OCR_ROLES:
        if "/" not in models[role]:
            raise ProjectError(f"models.{role} = {models[role]!r}: reading pages goes through OpenRouter, so it needs a vendor prefix")
    translations = []
    for entry in data.pop("translation"):
        entry = dict(entry)
        translations.append(_build(Translation, {**entry, "models": {**models, **entry.pop("models", {})}}))
    names = [t.language for t in translations]
    for t in translations:
        if t.cross_check and (t.cross_check not in names or names.index(t.cross_check) >= names.index(t.language)):
            raise ProjectError(f"{t.language}: cross_check {t.cross_check!r} must be a translation listed before it")
    return _build(Project, {**data, "dir": path.parent, "translations": translations, "models": models, "run": settings})


def _build(cls, values: dict):
    # A misspelt setting must not be silently ignored.
    try:
        return cls(**values)
    except TypeError as e:
        raise ProjectError(f"in the project file: {e}") from e
