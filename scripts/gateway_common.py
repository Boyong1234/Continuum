"""Local, explicit gateway configuration helpers. Never load QQ credentials."""
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PREFIXES=('CONTINUUM_BRIDGE_','CONTINUUM_GATEWAY_','ASTRBOT_BRIDGE_')


def local_env(path):
    path=Path(path).resolve()
    if not path.is_relative_to(ROOT/'data'):raise ValueError('Use project-local gateway configuration')
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):continue
        key,sep,value=line.partition('=')
        if not sep or not key.startswith(PREFIXES):raise ValueError('Unexpected gateway setting')
        os.environ[key]=value


def isolated(path):
    path=Path(path).resolve()
    if not path.is_relative_to(ROOT/'data'):raise ValueError('Project data paths only; no canonical switch')
    return path
