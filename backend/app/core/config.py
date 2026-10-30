import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from app.services.channels.config import OneBotConfig

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / '.env', override=False)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv('CONTINUUM_DATABASE_URL', f'sqlite:///{(ROOT / "data/continuum.db").as_posix()}'))
    persona_dir: Path = field(default_factory=lambda: Path(os.getenv('CONTINUUM_PERSONA_DIR', str(ROOT / 'data/persona'))))
    cors_origins: list[str] = field(default_factory=lambda: os.getenv('CONTINUUM_CORS_ORIGINS', 'http://127.0.0.1:5173,http://localhost:5173').split(','))
    context_budget: int = field(default_factory=lambda: int(os.getenv('CONTINUUM_CONTEXT_BUDGET', '12000')))
    chat_history_provider: str = field(default_factory=lambda: os.getenv('CONTINUUM_CHAT_HISTORY_PROVIDER', ''))
    natural_recall: bool = field(default_factory=lambda: os.getenv('CONTINUUM_NATURAL_RECALL', '0') == '1')
    dialogue_capabilities: bool = field(default_factory=lambda: os.getenv('CONTINUUM_DIALOGUE_CAPABILITIES', '0') == '1')
    style_references_enabled: bool = field(default_factory=lambda: os.getenv('CONTINUUM_STYLE_REFERENCES_ENABLED', '0') == '1')
    style_reference_index: Path | None = field(default_factory=lambda: ROOT / os.environ['CONTINUUM_STYLE_REFERENCE_INDEX'] if os.getenv('CONTINUUM_STYLE_REFERENCE_INDEX') else None)
    style_cloud_authorized: bool = field(default_factory=lambda: os.getenv('CONTINUUM_STYLE_CLOUD_AUTHORIZED', '0') == '1')
    correction_references_enabled: bool = field(default_factory=lambda: os.getenv('CONTINUUM_CORRECTION_REFERENCES_ENABLED', '0') == '1')
    correction_reference_index: Path | None = field(default_factory=lambda: ROOT / os.environ['CONTINUUM_CORRECTION_REFERENCE_INDEX'] if os.getenv('CONTINUUM_CORRECTION_REFERENCE_INDEX') else None)
    onebot: OneBotConfig = field(default_factory=OneBotConfig.from_environment)


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)
