# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Generic typed configuration utilities.

Recursively constructs typed dataclasses from raw dictionaries (e.g. parsed YAML),
with support for nested dataclasses, generics, unions, enums, and primitives.
"""

from __future__ import annotations

import copy
import functools
import logging
import types
from collections.abc import MutableMapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ------------------------------------------------------------------------------
# Flatten / unflatten
# ------------------------------------------------------------------------------


def flatten_config(
    config: dict[str, Any],
    separator: str = ".",
    flatten_list: bool = False,
    _parent_key: str = "",
) -> dict[str, Any]:
    """Flatten a nested dictionary into a dot-separated format."""
    args = (separator, flatten_list)
    items: list[tuple[str, Any]] = []
    for k, v in config.items():
        new_key = f"{_parent_key}{separator}{k}" if _parent_key else k
        if isinstance(v, MutableMapping):
            items.extend(flatten_config(v, *args, new_key).items())
        elif flatten_list and isinstance(v, list):
            for _k, _v in enumerate(v):
                items.extend(flatten_config({str(_k): _v}, *args, new_key).items())
        else:
            items.append((new_key, v))
    return dict(items)


def unflatten_config(config: dict[str, Any], separator: str = ".") -> dict[str, Any]:
    """Convert a flat dictionary into a nested dictionary."""
    nested: dict[str, Any] = {}
    for key, value in config.items():
        keys = key.split(separator)
        d = nested
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
    return nested


# ------------------------------------------------------------------------------
# Typed builder
# ------------------------------------------------------------------------------


@functools.cache
def _cached_type_hints(cls: type) -> dict[str, Any]:
    """Cache ``get_type_hints`` to avoid repeated evaluation of annotations."""
    return get_type_hints(cls)


def build_with_type_check(object_type: type[T], data: Any, *, deep_copy: bool = False) -> T:
    """Recursively initialize a typed object from a nested dictionary.

    Supports dataclasses (recursive), list[T], dict[K,V], X | None unions,
    Literal, Enum subclasses, and primitive types.
    """
    if deep_copy:
        data = copy.deepcopy(data)

    if data is None or object_type is Any:
        return data

    args = get_args(object_type)
    origin = get_origin(object_type)

    # dataclasses
    if is_dataclass(object_type):
        if not isinstance(data, dict):
            raise TypeError(f"Expected dict for {object_type.__name__}, got {type(data).__name__}")
        hints = _cached_type_hints(object_type)
        field_values: dict[str, Any] = {}
        consumed: set[str] = set()
        for f in fields(object_type):
            if not f.init:
                continue
            if f.name in data:
                field_values[f.name] = build_with_type_check(hints[f.name], data[f.name])
                consumed.add(f.name)
            else:
                logger.debug("Field '%s' not found in data for %s.", f.name, object_type.__name__)
        for key in data:
            if key not in consumed:
                logger.warning("Field '%s' ignored when initializing %s.", key, object_type.__name__)
        return object_type(**field_values)

    # list[T]
    if origin is list and len(args) == 1:
        if not isinstance(data, list):
            raise TypeError(f"Expected list for {object_type}, got {type(data).__name__}")
        return [build_with_type_check(args[0], item) for item in data]

    # dict[K, V]
    if origin is dict and len(args) == 2:
        if not isinstance(data, dict):
            raise TypeError(f"Expected dict for {object_type}, got {type(data).__name__}")
        return {build_with_type_check(args[0], k): build_with_type_check(args[1], v) for k, v in data.items()}

    # Union / X | None  (typing.Union for Optional[X], types.UnionType for X | None)
    if origin is Union or origin is types.UnionType:
        for arg in args:
            if arg is type(None) and data is None:
                return None
            if arg is type(None):
                continue
            try:
                return build_with_type_check(arg, data)
            except (TypeError, ValueError):
                continue

    # Literal
    if origin is Literal:
        if data not in args:
            raise ValueError(f"Value {data!r} is not a valid literal for {object_type}.")
        return data

    # Enum
    if isinstance(object_type, type) and issubclass(object_type, Enum):
        return object_type(data)

    # Primitive types (str, int, float, bool, Path, ...)
    try:
        return object_type(data)
    except (TypeError, ValueError) as e:
        raise TypeError(f"Failed to initialize {object_type} with {data!r}: {e}") from e
