from pathlib import Path

import yaml


class PersonaManager:
    def __init__(self, directory: Path):
        self.directory = directory

    def read(self) -> dict:
        return {
            'core': (self.directory / 'core.md').read_text(encoding='utf-8'),
            'preferences': yaml.safe_load((self.directory / 'preferences.yaml').read_text(encoding='utf-8')) or {},
            'examples': (self.directory / 'examples.md').read_text(encoding='utf-8'),
        }
