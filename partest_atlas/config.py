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
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from partest_atlas.artifacts import Scope

CONFIG_NAME = "atlas.yaml"

#: Куда складывать артефакты, если в файле не сказано иначе.
DEFAULT_ARTIFACTS_DIR = "atlas"


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
        raw = self.raw.get("artifacts_dir") or DEFAULT_ARTIFACTS_DIR
        path = Path(raw)
        return path if path.is_absolute() else self.root / path

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
        raise ConfigError(
            f"{path} есть, но PyYAML не установлен — прочитать нечем. "
            "Поставьте partest-atlas с любым extra либо PyYAML отдельно"
        ) from exc

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"{path}: не разбирается как YAML — {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: ожидался объект на верхнем уровне, получено {type(raw).__name__}")

    return Config(root=path.parent, raw=raw)


def env(name: str, default: str = "") -> str:
    """Значение переменной окружения, названной в конфигурации.

    Адреса, логины и пароли в `atlas.yaml` не пишутся: файл называет **имя переменной**,
    значение приходит из окружения. Файл лежит в репозитории и попадает в историю —
    это первая причина. Вторая прозаичнее: один и тот же файл обязан работать и на
    машине разработчика, и на конвейере, а отличаются они окружением.
    """
    return os.getenv(name, default) or default
