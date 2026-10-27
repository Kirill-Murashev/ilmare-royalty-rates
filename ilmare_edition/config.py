"""Operator configuration and input/output boundary checks."""

from dataclasses import dataclass, field
from pathlib import Path

# Layout of the Hugging Face dataset irlspbru/RFSD (version 3.1.0) after `hf download`.
DEFAULT_PATTERN = "RFSD/year={year}/part-0.parquet"
DEFAULT_MODEL = Path(__file__).parent / "data" / "share_model_v2.json"


def parse_years(value: str) -> tuple[int, ...]:
    """Accept a closed range or a comma-separated list of statement years."""
    try:
        if "-" in value:
            start, end = map(int, value.split("-"))
            years = tuple(range(start, end + 1))
        else:
            years = tuple(sorted({int(part) for part in value.split(",")}))
    except ValueError as exc:
        raise ValueError("years must be a range such as 2011-2025 or a list") from exc
    if not years or min(years) < 1:
        raise ValueError("years must be nonempty and positive")
    return years


@dataclass(frozen=True)
class Config:
    data_root: Path
    okved_ref: Path
    out: Path
    edition: str
    years: tuple[int, ...] = field(default_factory=lambda: tuple(range(2011, 2026)))
    file_pattern: str = DEFAULT_PATTERN
    share_model: Path = DEFAULT_MODEL
    seed: int = 20261017
    draws: int = 100000
    threads: int = 3
    memory_limit: str = "3GB"
    code_version: str | None = None

    def input_path(self, year: int) -> Path:
        path = (self.data_root / self.file_pattern.format(year=year)).resolve()
        if not path.is_relative_to(self.data_root.resolve()):
            raise ValueError("input file must be inside data-root")
        if path.suffix != ".parquet":
            raise ValueError("file-pattern must select only .parquet files")
        return path

    def validate(self) -> None:
        root, out = self.data_root.resolve(), self.out.resolve()
        if out.is_relative_to(root):
            raise ValueError("out must not be inside data-root")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise ValueError("out must not exist or must be an empty directory")
        if not root.is_dir():
            raise ValueError("data-root must be a directory")
        if not self.years or self.draws < 1 or self.threads < 1 or self.seed < 0:
            raise ValueError("years, draws and threads must be positive; seed nonnegative")
        for path in (self.okved_ref, self.share_model):
            if not path.is_file():
                raise ValueError(f"input not found: {path}")
        for year in self.years:
            if not self.input_path(year).is_file():
                raise ValueError(f"statement file not found for {year}")
