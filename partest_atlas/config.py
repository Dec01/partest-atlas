"""`atlas.yaml` — единственный файл, в котором живёт проектное знание.

Правило, которому подчинён весь пакет: **код сборщиков про конкретный проект не знает
ничего.** Перенос сбора на второй проект — это новый `atlas.yaml`, а не новый код. Если
ради второго проекта пришлось править сборщик, проектное знание протекло в пакет, и это
дефект пакета.

Прототип, из которого идёт перенос, это правило нарушал в одном месте: путь к функции
соединения проекта был вписан в сборщик схемы константой. Здесь он приходит из
конфигурации — см. `database.connect`.

Файл необязателен целиком, и каждый раздел в нём необязателен. Нет раздела — нет
сборщика; это рабочее состояние, а не ошибка. Набор разделов говорит, **что проект готов
показать**, и ничего не обещает сверх этого.

Публичный интерфейс — имена из ``__all__``: им пользуются и чужие программы (сервер карты
читает тот же `atlas.yaml` со своими разделами), поэтому они меняются только с записью в
``CHANGELOG.md``. Разделы, которых пакет не знает, :class:`Config` отдаёт как есть
(:meth:`Config.section`) — они принадлежат тому, кто их читает.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from partest_atlas.artifacts import Scope
from partest_atlas.lang import t

__all__ = [
    "CONFIG_NAME",
    "DEFAULT_ARTIFACTS_DIR",
    "Config",
    "ConfigError",
    "KNOWN_KEYS",
    "env",
    "find",
    "load",
    "non_negative",
]

CONFIG_NAME = "atlas.yaml"

#: Куда складывать артефакты, если в файле не сказано иначе.
DEFAULT_ARTIFACTS_DIR = "atlas"

#: Разделы, которыми владеет этот пакет, и ключи, которые он в них читает (договор
#: `atlas-config`). Неизвестный ключ своего раздела — называется (:meth:`Config.unknown_keys`):
#: опечатка `schemaz:` иначе молча оставила бы умолчание. Чужие разделы (`reports`,
#: `runner`… — сервер карты) не отвергаются и не проверяются: они не наши.
KNOWN_KEYS: Dict[str, frozenset] = {
    "project": frozenset({"name", "api_prefix"}),
    "database": frozenset({"connect", "dsn_env", "schemas", "key", "label", "exclude",
                           "count_rows", "connect_timeout"}),
    "container_logs": frozenset({"source", "rules"}),
    "db_effects": frozenset({"settle_sec", "per_test_limit", "include", "phases", "keep_runs"}),
    "bus_flow": frozenset({"reader", "configured", "include", "settle_sec", "keep_runs"}),
    "ui_flow": frozenset({"window_ms", "ignore_url", "ignore_control"}),
}


def _is_unc(text: str) -> bool:
    """Путь UNC (`\\\\сервер\\ресурс`, `//сервер/ресурс`): общий ресурс сети. Договор
    `atlas-config` их не принимает — запись в чужой ресурс не своя папка артефактов."""
    return text.startswith(("\\\\", "//"))


class ConfigError(RuntimeError):
    """Конфигурация есть, но прочитать её нельзя.

    Отдельно от «файла нет»: отсутствие файла — законное состояние проекта, который
    ещё ничего не настроил, а битый файл — ошибка, которую надо показать. Сводить их к
    одному значит выдать неработающую настройку за отсутствующую.
    """


@dataclass(frozen=True)
class Config:
    """Прочитанный `atlas.yaml`. Разделы отдаются как есть — толкует их сборщик."""

    root: Path
    raw: Dict[str, Any]

    @property
    def project(self) -> Dict[str, Any]:
        return dict(self.raw.get("project") or {})

    @property
    def artifacts_dir(self) -> Path:
        """Каталог артефактов: относительный — от каталога `atlas.yaml`, абсолютный — как
        есть, UNC — :class:`ConfigError` (значение не показывается)."""
        raw = self.raw.get("artifacts_dir") or DEFAULT_ARTIFACTS_DIR
        if not isinstance(raw, str):
            raise ConfigError(t("artifacts_dir: ожидался путь строкой",
                                "artifacts_dir: expected a path as a string"))
        if _is_unc(raw.strip()):
            raise ConfigError(t("artifacts_dir: пути UNC (общий ресурс сети) не принимаются",
                                "artifacts_dir: UNC paths (network shares) are not accepted"))
        path = Path(raw)
        return path if path.is_absolute() else self.root / path

    def unknown_keys(self) -> List[str]:
        """Ключи своих разделов, которых пакет не читает: ``["database.schemaz", …]``."""
        out: List[str] = []
        for name, known in KNOWN_KEYS.items():
            section = self.raw.get(name)
            if isinstance(section, dict):
                out.extend(f"{name}.{key}" for key in section if key not in known)
        return out

    @property
    def scope(self) -> Scope:
        """Область берётся из окружения, а не из файла.

        `atlas.yaml` лежит в репозитории и одинаков у разработчика и на конвейере;
        различаются они контуром. Запиши контур в файл — и второй контур потребует
        второй файл либо молча затрёт артефакты первого.
        """
        return Scope.from_env()

    def section(self, name: str) -> Dict[str, Any]:
        return dict(self.raw.get(name) or {})

    def path_for(self, kind: str) -> Path:
        """Куда класть артефакт этого вида — с учётом раскладки по системе и контуру."""
        return self.scope.path(self.artifacts_dir, kind)


def non_negative(
    section: Dict[str, Any], key: str, default: float, kind: type, *, where: str
) -> Any:
    """Конечное неотрицательное число из раздела. Нет ключа — умолчание; `0` — это `0`,
    а не умолчание; кривое значение (в том числе `.nan` и `.inf` YAML) — `ValueError`
    с именем ключа: вызывающий сообщает о нём как об ошибке конфигурации, а не роняет
    прогон INTERNALERROR или вечным сном."""
    raw = section.get(key)
    if raw is None:
        return kind(default)
    try:
        if isinstance(raw, bool):
            raise ValueError
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ValueError
        value = kind(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError
        if kind is int and isinstance(raw, float) and raw != value:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        # Значение не показывается (договор `atlas-config`): называется ключ и что ждали.
        raise ValueError(t(f"{where}.{key}: ожидалось неотрицательное число",
                           f"{where}.{key}: expected a non-negative number")) from None
    return value


def find(start: Optional[Path] = None) -> Optional[Path]:
    """Найти `atlas.yaml`, поднимаясь от *start* к корню. `None`, если файла нет.

    Поиск вверх, а не только в текущем каталоге: сборщик зовут и из корня проекта, и из
    каталога тестов, и вручную откуда придётся.
    """
    current = Path(start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        path = candidate / CONFIG_NAME
        if path.is_file():
            return path
    return None


def load(start: Optional[Path] = None) -> Config:
    """Прочитать конфигурацию. Файла нет — пустая конфигурация со значениями по умолчанию.

    Пустая конфигурация рабочая: артефакты лягут в `atlas/`, разделов не будет ни одного,
    и ни один сборщик не запустится. Это честное «проект ещё ничего не настроил».
    """
    path = find(start)
    if path is None:
        return Config(root=Path(start or Path.cwd()).resolve(), raw={})

    try:
        import yaml  # noqa: PLC0415 — зависимость нужна только тем, у кого есть файл
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ConfigError(t(
            f"{path} есть, но PyYAML не установлен — прочитать нечем. "
            "Поставьте partest-atlas с любым extra либо PyYAML отдельно",
            f"{path} exists, but PyYAML is not installed to read it. "
            "Install partest-atlas with any extra, or PyYAML on its own",
        )) from exc

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(t(f"{path}: не разбирается как YAML — {exc}",
                            f"{path}: not valid YAML — {exc}")) from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(t(f"{path}: ожидался объект на верхнем уровне, получено {type(raw).__name__}",
                            f"{path}: expected an object at the top level, got {type(raw).__name__}"))
    for name in KNOWN_KEYS:
        if raw.get(name) is not None and not isinstance(raw.get(name), dict):
            raise ConfigError(t(f"{path}: раздел {name} — не объект",
                                f"{path}: section {name} is not an object"))

    cfg = Config(root=path.parent, raw=raw)
    cfg.artifacts_dir  # noqa: B018 — UNC и не строка отвергаются сразу, а не при записи
    return cfg


def env(name: str, default: str = "") -> str:
    """Значение переменной окружения, названной в конфигурации.

    Адреса, логины и пароли в `atlas.yaml` не пишутся: файл называет **имя переменной**,
    значение приходит из окружения. Файл лежит в репозитории и попадает в историю —
    это первая причина. Вторая прозаичнее: один и тот же файл обязан работать и на
    машине разработчика, и на конвейере, а отличаются они окружением.
    """
    return os.getenv(name, default) or default
