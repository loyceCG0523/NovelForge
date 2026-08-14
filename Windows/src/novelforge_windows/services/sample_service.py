"""Local sample import and preview service."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from novelforge_windows.domain.models import SampleDocument
from novelforge_windows.infrastructure.repositories import SampleRepository


MAX_SAMPLE_BYTES = 100 * 1024 * 1024
SUPPORTED_SUFFIXES = {".txt", ".md"}


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请转换为 UTF-8、GBK 或 GB18030。")


class SampleService:
    def __init__(self, samples: SampleRepository, samples_dir: Path) -> None:
        self.samples = samples
        self.samples_dir = samples_dir.resolve()

    def list_samples(self) -> list[SampleDocument]:
        return self.samples.list()

    def import_document(self, source: Path) -> SampleDocument:
        source = Path(source).resolve()
        if not source.is_file():
            raise ValueError("样本文件不存在。")
        if source.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError("仅支持 TXT 和 Markdown 文件。")
        if source.stat().st_size > MAX_SAMPLE_BYTES:
            raise ValueError("样本文件不能超过 100 MB。")
        text = _decode_text(source.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
        sample_id = str(uuid4())
        destination = self.samples_dir / f"{sample_id}.txt"
        destination.write_text(text, encoding="utf-8")
        try:
            return self.samples.create(
                title=source.stem,
                source_name=source.name,
                local_path=destination,
                character_count=len("".join(text.split())),
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def preview(self, sample_id: str, *, maximum_chars: int = 20_000) -> str:
        sample = self.samples.get(sample_id)
        if sample is None:
            raise LookupError("样本不存在。")
        path = Path(sample.local_path)
        if not path.is_file():
            raise FileNotFoundError("样本的本地文件已经丢失。")
        with path.open("r", encoding="utf-8") as handle:
            return handle.read(maximum_chars)

    def delete_sample(self, sample_id: str) -> None:
        sample = self.samples.delete(sample_id)
        path = Path(sample.local_path).resolve()
        try:
            path.relative_to(self.samples_dir)
        except ValueError:
            return
        path.unlink(missing_ok=True)

