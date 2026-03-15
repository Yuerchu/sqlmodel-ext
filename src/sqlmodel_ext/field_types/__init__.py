"""
sqlmodel_ext.field_types -- Reusable type aliases and custom types for SQLModel.

Provides constrained string/numeric types, path types, URL types, and IP address types,
all compatible with Pydantic validation and SQLAlchemy column mapping.
"""
from pathlib import Path
from typing import Annotated, TypeAlias

from pydantic import StringConstraints
from sqlalchemy import BigInteger
from sqlmodel import Field

from ._internal.path import _DirectoryPathHandler, _FilePathHandler
from .ip_address import IPAddress
from .mixins import ModuleNameMixin
from .url import HttpUrl, SafeHttpUrl, Url, WebSocketUrl

# Re-export SSRF utilities
from ._ssrf import UnsafeURLError, validate_not_private_host

# ---------------------------------------------------------------------------
#  Public, Database-Agnostic Types
# ---------------------------------------------------------------------------

DirectoryPathType = Annotated[Path, _DirectoryPathHandler]
"""
A directory path type compatible with Pydantic and SQLModel.

Validates that the path should not contain a file extension,
while behaving as a ``pathlib.Path`` in Python code.
"""

FilePathType = Annotated[Path, _FilePathHandler]
"""
A file path type compatible with Pydantic and SQLModel.

Validates that the path must contain a filename component,
while behaving as a ``pathlib.Path`` in Python code.
"""


# ---------------------------------------------------------------------------
#  Field Constraint Type Aliases (Annotated Style)
# ---------------------------------------------------------------------------

_NO_NULL_BYTE = StringConstraints(pattern=r'^[^\x00]*$')
"""PostgreSQL rejects null bytes in text columns. pydantic-core compiles the regex once with zero Python overhead."""

# String length constraints
Str24: TypeAlias = Annotated[str, Field(max_length=24), _NO_NULL_BYTE]
"""24-character string field"""

Str32: TypeAlias = Annotated[str, Field(max_length=32), _NO_NULL_BYTE]
"""32-character string field"""

Str36: TypeAlias = Annotated[str, Field(max_length=36), _NO_NULL_BYTE]
"""36-character string field (UUID standard format length)"""

Str48: TypeAlias = Annotated[str, Field(max_length=48), _NO_NULL_BYTE]
"""48-character string field"""

Str64: TypeAlias = Annotated[str, Field(max_length=64), _NO_NULL_BYTE]
"""64-character string field"""

Str100: TypeAlias = Annotated[str, Field(max_length=100), _NO_NULL_BYTE]
"""100-character string field"""

Str128: TypeAlias = Annotated[str, Field(max_length=128), _NO_NULL_BYTE]
"""128-character string field"""

Str255: TypeAlias = Annotated[str, Field(max_length=255), _NO_NULL_BYTE]
"""255-character string field"""

Str256: TypeAlias = Annotated[str, Field(max_length=256), _NO_NULL_BYTE]
"""256-character string field"""

Str500: TypeAlias = Annotated[str, Field(max_length=500), _NO_NULL_BYTE]
"""500-character string field"""

Str512: TypeAlias = Annotated[str, Field(max_length=512), _NO_NULL_BYTE]
"""512-character string field"""

Str2048: TypeAlias = Annotated[str, Field(max_length=2048), _NO_NULL_BYTE]
"""2048-character string field (URLs etc.)"""

Text1K: TypeAlias = Annotated[str, Field(max_length=1000), _NO_NULL_BYTE]
"""1000-character text field"""

Text1024: TypeAlias = Annotated[str, Field(max_length=1024), _NO_NULL_BYTE]
"""1024-character text field"""

Text2K: TypeAlias = Annotated[str, Field(max_length=2000), _NO_NULL_BYTE]
"""2000-character text field"""

Text2500: TypeAlias = Annotated[str, Field(max_length=2500), _NO_NULL_BYTE]
"""2500-character text field"""

Text3K: TypeAlias = Annotated[str, Field(max_length=3000), _NO_NULL_BYTE]
"""3000-character text field"""

Text5K: TypeAlias = Annotated[str, Field(max_length=5000), _NO_NULL_BYTE]
"""5000-character text field"""

Text10K: TypeAlias = Annotated[str, Field(max_length=10000), _NO_NULL_BYTE]
"""10000-character text field"""

Text60K: TypeAlias = Annotated[str, Field(max_length=60000), _NO_NULL_BYTE]
"""60000-character text field"""

Text64K: TypeAlias = Annotated[str, Field(max_length=65536), _NO_NULL_BYTE]
"""65536-character text field"""

Text100K: TypeAlias = Annotated[str, Field(max_length=100000), _NO_NULL_BYTE]
"""100000-character text field"""

Text1M: TypeAlias = Annotated[str, Field(max_length=1000000), _NO_NULL_BYTE]
"""1000000-character text field (tool call parameters, tool responses, etc.)"""

# Numeric range constraints
Port: TypeAlias = Annotated[int, Field(ge=1, le=65535)]
"""Port number (1-65535)"""

Percentage: TypeAlias = Annotated[int, Field(ge=0, le=100)]
"""Percentage (0-100)"""

INT32_MAX = 2147483647
"""Maximum value for PostgreSQL INTEGER column (2^31-1)"""

PositiveInt: TypeAlias = Annotated[int, Field(ge=1, le=INT32_MAX)]
"""Positive integer (1 to 2147483647, fits PostgreSQL INTEGER)"""

NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0, le=INT32_MAX)]
"""Non-negative integer (0 to 2147483647, fits PostgreSQL INTEGER)"""

PositiveBigInt: TypeAlias = Annotated[int, Field(ge=1, sa_type=BigInteger)]
"""Positive big integer (>=1, BigInteger storage)"""

NonNegativeBigInt: TypeAlias = Annotated[int, Field(ge=0, sa_type=BigInteger)]
"""Non-negative big integer (>=0, BigInteger storage)"""

PositiveFloat: TypeAlias = Annotated[float, Field(gt=0.0)]
"""Positive float (>0)"""
