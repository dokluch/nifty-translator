"""Turns sources into pages: one original and one contrast-normalised JPEG per scanned page,
or one text file per source that has already been through OCR."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic"}
TEXTS = {".txt", ".md"}
SOURCES = {".pdf", *IMAGES, *TEXTS}


def page_count(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    return int(re.search(r"^Pages:\s+(\d+)", out, re.M).group(1))


def extract_page(pdf: Path, page: int, dest: Path) -> None:
    """Keeps the embedded JPEG untouched when the page is one image; otherwise renders it."""
    with tempfile.TemporaryDirectory() as tmp:
        prefix = f"{tmp}/p"
        subprocess.run(["pdfimages", "-j", "-f", str(page), "-l", str(page), str(pdf), prefix], check=True)
        images = sorted(Path(tmp).glob("p-*"))
        if len(images) == 1 and images[0].suffix == ".jpg":
            shutil.move(images[0], dest)
            return
        subprocess.run(["pdftoppm", "-r", "300", "-jpeg", "-jpegopt", "quality=92", "-singlefile",
                        "-f", str(page), "-l", str(page), str(pdf), str(dest.with_suffix(""))], check=True)


def copy_image(src: Path, dest: Path) -> None:
    if src.suffix.lower() in (".jpg", ".jpeg"):
        shutil.copy(src, dest)
    else:
        subprocess.run(["magick", str(src), "-quality", "92", str(dest)], check=True)


def enhance(src: Path, dest: Path) -> None:
    """Greyscale, divided by its own background: removes colour casts and glare gradients.
    Morphological closing erases the text, leaving only the page's lighting to divide by."""
    subprocess.run([
        "magick", str(src), "-colorspace", "Gray",
        "(", "+clone", "-resize", "25%", "-morphology", "Close", "Disk:7",
        "-blur", "0x12", "-resize", "400%", ")", "-compose", "Divide_Src", "-composite",
        "-level", "45%,98%", "-quality", "85", str(dest),
    ], check=True)


def extract_book(sources: list[Path], pages_dir: Path) -> list[str]:
    """Numbers pages across the sources in order. Returns page ids like 'p0007'."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    ids = []
    for source in sources:
        kind = source.suffix.lower()
        count = page_count(source) if kind == ".pdf" else 1
        for n in range(1, count + 1):
            pid = f"p{len(ids) + 1:04d}"
            ids.append(pid)
            if kind in TEXTS:
                # Read every run, so a correction to the text reaches the sections it touches.
                shutil.copy(source, pages_dir / f"{pid}.txt")
                continue
            orig, enh = pages_dir / f"{pid}.jpg", pages_dir / f"{pid}.enh.jpg"
            if not orig.exists():
                extract_page(source, n, orig) if kind == ".pdf" else copy_image(source, orig)
            if not enh.exists():
                enhance(orig, enh)
    return ids
