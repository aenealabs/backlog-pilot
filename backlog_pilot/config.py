"""Runtime settings, read from environment variables (and an optional .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
DATA_DIR = REPO_DIR / "data"


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    model: str
    record: bool
    db_path: str
    replay_dir: Path
    scenarios_path: Path
    sample_backlog_path: Path
    users_path: Path
    personas_path: Path
    products_path: Path
    audit_questions_path: Path


def load_settings() -> Settings:
    load_dotenv(REPO_DIR / ".env")
    return Settings(
        llm_provider=os.getenv("BP_LLM_PROVIDER", "replay").strip().lower(),
        model=os.getenv("BP_MODEL", "claude-opus-5-5"),
        record=_flag("BP_RECORD"),
        db_path=os.getenv("BP_DB_PATH", str(REPO_DIR / "backlog_pilot.db")),
        replay_dir=DATA_DIR / "replays",
        scenarios_path=DATA_DIR / "scenarios.json",
        sample_backlog_path=DATA_DIR / "sample_backlog.json",
        users_path=DATA_DIR / "users.json",
        personas_path=DATA_DIR / "personas.json",
        products_path=DATA_DIR / "products.json",
        audit_questions_path=DATA_DIR / "audit_questions.json",
    )
