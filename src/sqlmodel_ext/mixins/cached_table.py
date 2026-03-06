"""
Cached Table Mixin -- Redis-based query caching for SQLModel table models.

Adds an L1 Redis cache layer to ``TableBaseMixin.get()`` queries.
Inherit ``CachedTableBaseMixin`` to enable; do not inherit if caching is not needed.

Design principles:
- Rich model: cache read/write and invalidation logic are cohesive within the mixin.
- Redis client managed externally: user must call ``CachedTableBaseMixin.configure_redis()``
  at application startup to supply an ``redis.asyncio.Redis`` instance.
- Fail-fast: if Redis is not configured, ``RuntimeError`` is raised.
  Runtime Redis errors degrade gracefully to DB (logged via stdlib logging).
- Full-row caching: always caches all column fields; no partial-field caching.
- Serialization: ``model_dump_json()`` -> ``json.loads`` -> ``model_validate``
  (ensures ``_sa_instance_state`` is correctly created).
- Explicit failure: ``no_cache`` parameter only exists on ``CachedTableBaseMixin.get()``;
  passing it to a non-cached model raises ``TypeError`` (natural Python behavior).

Two-tier cache architecture:
- ID cache (``id:{ModelName}:{id_value}``): single-row ID equality queries, row-level invalidation.
- Query cache (``query:{ModelName}:{hash}``): condition/list queries, model-level invalidation.

Invalidation granularity:
- save/update: row-level DEL ``id:{cls}:{id}`` + model-level SCAN+DEL ``query:{cls}:*``
- delete(instances): row-level DEL per instance + model-level SCAN+DEL ``query:*``
- delete(condition): model-level SCAN+DEL (``id:*`` + ``query:*``)
- STI polymorphic: subclass changes also invalidate all cached ancestor ID/query caches.

Cache skip conditions:
- ``no_cache=True`` (caller explicitly skips)
- ``load`` contains non-MANYTOONE or non-cacheable relations (cannot use multi-ID cache optimization)
- ``options is not None`` (ExecutableOption changes loading behavior)
- ``with_for_update`` (pessimistic lock must read latest)
- ``populate_existing`` (explicitly requests identity map refresh)
- ``join is not None`` (join target changes don't trigger main model invalidation; phantom read risk)

Dependencies (optional)::

    pip install redis orjson
    # or: pip install sqlmodel-ext[cache]

Usage::

    from redis.asyncio import Redis
    from sqlmodel_ext.mixins.cached_table import CachedTableBaseMixin

    # At startup:
    redis_client = Redis.from_url("redis://localhost:6379/0", decode_responses=False)
    CachedTableBaseMixin.configure_redis(redis_client)

    # Define your model:
    class Character(CachedTableBaseMixin, CharacterBase, UUIDTableBaseMixin, table=True):
        __cache_ttl__: ClassVar[int] = 1800  # 30 minutes
"""
import ast
import asyncio
import hashlib
import inspect
import json
import logging
import textwrap
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self, cast, overload

from pydantic import ValidationError
from sqlalchemy import ColumnElement, event, inspect as sa_inspect
from sqlalchemy.orm import (
    InstanceState,
    QueryableAttribute,
    Session as _SyncSession,
    make_transient_to_detached,
)
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.relationships import MANYTOONE  # pyright: ignore[reportPrivateImportUsage]
from sqlalchemy.sql import operators
from sqlalchemy.sql.base import ExecutableOption
from sqlalchemy.sql.elements import BinaryExpression
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlalchemy.sql._typing import _OnClauseArgument  # pyright: ignore[reportPrivateUsage]

from sqlmodel_ext.base import SQLModelBase
from sqlmodel_ext.mixins.polymorphic import PolymorphicBaseMixin
from sqlmodel_ext.mixins.table import TableBaseMixin
from sqlmodel_ext.pagination import TableViewRequest

# Optional dependency: orjson (falls back to stdlib json)
try:
    import orjson as _json_lib

    def _json_dumps(obj: Any) -> bytes:
        return _json_lib.dumps(obj)

    def _json_loads(data: bytes | str) -> Any:
        return _json_lib.loads(data)

except ImportError:
    _json_lib = None  # type: ignore[assignment]

    def _json_dumps(obj: Any) -> bytes:
        return json.dumps(obj, separators=(',', ':')).encode('utf-8')

    def _json_loads(data: bytes | str) -> Any:
        return json.loads(data)


logger = logging.getLogger(__name__)


class _CacheResultType(StrEnum):
    """Cache serialization wrapper type -- distinguishes None/single/list query results."""
    NONE = 'none'
    LIST = 'list'
    SINGLE = 'single'


# Serialization wrapper JSON field names
_WRAPPER_TYPE_KEY = '_t'
_WRAPPER_ITEMS_KEY = '_items'
_WRAPPER_DATA_KEY = '_data'
_WRAPPER_CLASS_KEY = '_c'  # Actual class name (polymorphic-safe deserialization)

# session.info keys -- cache invalidation state tracking
_SESSION_PENDING_CACHE_KEY = '_pending_cache_invalidation_types'
_SESSION_SYNCED_CACHE_KEY = '_synced_cache_invalidation_types'

# Sentinel -- add() scenario: new item has no old ID cache to invalidate, only query caches
_QUERY_ONLY_INVALIDATION = object()

# Sentinel -- delete(condition) scenario: no specific IDs, needs model-level full invalidation
_FULL_MODEL_INVALIDATION = object()

# Sentinel -- _try_load_from_id_caches() return value, means cache miss (distinct from None result)
_LOAD_CACHE_MISS = object()

# Method names that subclasses must not call directly.
# check_cache_config() uses AST inspection to prevent post-commit access to expired attributes.
_FORBIDDEN_DIRECT_CALLS: frozenset[str] = frozenset({
    'invalidate_by_id',
    'invalidate_all',
    '_invalidate_for_model',
    '_invalidate_id_cache',
    '_invalidate_query_caches',
})


class CachedTableBaseMixin(TableBaseMixin):
    """
    Inherit this mixin to enable Redis query caching. Do not inherit if caching is not needed.

    MRO: Model -> CachedTableBaseMixin -> Base -> TableBaseMixin

    ClassVar configuration:
        __cache_ttl__: Cache TTL in seconds. Override per-model as needed.

    Redis must be configured before use::

        from redis.asyncio import Redis
        CachedTableBaseMixin.configure_redis(
            Redis.from_url("redis://localhost:6379/0", decode_responses=False)
        )
    """

    __cache_ttl__: ClassVar[int] = 3600
    """Cache TTL in seconds. Override via class definition to customize."""

    _commit_hook_registered: ClassVar[bool] = False
    """Whether the after_commit event hook has been registered."""

    _redis_client: ClassVar[Any] = None
    """
    Redis client instance (``redis.asyncio.Redis[bytes]``).
    Set via ``configure_redis()`` at application startup.
    Typed as Any because redis is an optional dependency.
    """

    # ---- Internal constants ----
    _CACHE_KEY_PREFIX: ClassVar[str] = 'query'
    """Query cache key prefix. Format: query:{ModelName}:{hash}"""

    _ID_CACHE_KEY_PREFIX: ClassVar[str] = 'id'
    """ID cache key prefix. Format: id:{ModelName}:{id_value}"""

    _CACHE_KEY_HASH_LENGTH: ClassVar[int] = 16
    """MD5 hash truncation length for cache keys."""

    _SCAN_BATCH_SIZE: ClassVar[int] = 100
    """Redis SCAN command count parameter per iteration."""

    _subclass_name_cache: ClassVar[dict[str, type]] = {}
    """class_name -> type cache, avoids _resolve_subclass() recursive traversal each time."""

    # ================================================================
    #  Redis client management
    # ================================================================

    @classmethod
    def configure_redis(cls, client: Any) -> None:
        """
        Configure the Redis client used for caching.

        Must be called once at application startup before any cache operations.

        :param client: An ``redis.asyncio.Redis`` instance (decode_responses=False recommended).
        :raises TypeError: If client is None.
        """
        if client is None:
            raise TypeError("Redis client cannot be None")
        cls._redis_client = client
        logger.debug("CachedTableBaseMixin: Redis client configured")

    @classmethod
    def _get_client(cls) -> Any:
        """
        Get the cache Redis client (``redis.asyncio.Redis[bytes]``).

        :raises RuntimeError: If Redis has not been configured via ``configure_redis()``.
        :returns: The Redis client (typed as Any since redis is an optional dependency).
        """
        if cls._redis_client is None:
            raise RuntimeError(
                "CachedTableBaseMixin: Redis not configured. "
                "Call CachedTableBaseMixin.configure_redis(client) at startup."
            )
        return cls._redis_client

    # ================================================================
    #  Cache primitives (runtime Redis errors -> log + degrade)
    # ================================================================

    @classmethod
    async def _cache_get(cls, key: str) -> bytes | None:
        """Read from cache. On Redis error: log + return None (degrade to DB)."""
        try:
            return await cls._get_client().get(key)
        except RuntimeError:
            raise  # Not initialized: fail fast
        except Exception as e:
            logger.error("Redis read error key='%s': %s", key, e)
            return None

    @classmethod
    async def _cache_set(cls, key: str, value: bytes, ttl: int) -> None:
        """Write to cache. On Redis error: log + skip (non-critical path)."""
        try:
            await cls._get_client().set(key, value, ex=ttl)
        except RuntimeError:
            raise
        except Exception as e:
            logger.error("Redis write error key='%s': %s", key, e)

    @classmethod
    async def _cache_delete(cls, key: str) -> None:
        """
        Delete from cache.

        RuntimeError (not initialized) propagates directly.
        Other exceptions also propagate so the sync path can detect
        failure and avoid marking as synced, allowing compensating retry.
        """
        await cls._get_client().delete(key)

    @classmethod
    async def _cache_delete_pattern(cls, pattern: str) -> None:
        """
        SCAN + DEL pattern-based deletion. Avoids KEYS blocking.

        Exceptions propagate (same as ``_cache_delete``).
        """
        client = cls._get_client()
        cursor = 0
        while True:
            cursor, keys = await client.scan(
                cursor, match=pattern, count=cls._SCAN_BATCH_SIZE,
            )
            if keys:
                await client.delete(*keys)
            if cursor == 0:
                break

    # ================================================================
    #  Static checks
    # ================================================================

    @classmethod
    def check_cache_config(cls) -> None:
        """
        Validate all CachedTableBaseMixin subclass configurations and register session event hooks.

        Call once at application startup (after ``configure_redis()``).

        Checks:
        1. Redis cache client is available.
        2. Subclasses (recursive) must not override ``_get_client`` (breaks Redis access).
        3. ``__cache_ttl__`` must be a positive integer.
        4. Subclass methods must not directly call cache invalidation methods (AST check).

        Side effects:
        - Registers SQLAlchemy Session after_commit/after_rollback event hooks.
        """
        # Verify Redis cache client is available
        _ = cls._get_client()

        violations: list[str] = []

        def _check_forbidden_calls(sub: type) -> None:
            """
            AST-check subclass method bodies for direct cache invalidation calls.

            Subclass methods needing cache invalidation after bypassing CRUD should use:
            - ``_register_pending_invalidation()`` to register pending IDs
            - ``_commit_and_invalidate()`` or ``_sync_invalidate_after_commit()`` to execute
            Directly calling ``invalidate_by_id()`` etc. may cause MissingGreenlet
            from accessing expired attributes after commit.
            """
            for attr_name, attr in sub.__dict__.items():
                # Unwrap descriptors, collect all function objects to scan
                funcs: list[Any] = []
                if isinstance(attr, (classmethod, staticmethod)):
                    funcs.append(attr.__func__)
                elif isinstance(attr, property):
                    for accessor in (attr.fget, attr.fset, attr.fdel):
                        if accessor is not None:
                            funcs.append(accessor)
                elif inspect.isfunction(attr):
                    funcs.append(attr)
                seen: set[str] = set()
                for func_obj in funcs:
                    try:
                        source = textwrap.dedent(inspect.getsource(func_obj))
                        tree = ast.parse(source)
                    except (OSError, TypeError, SyntaxError):
                        continue
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.Call):
                            continue
                        func = node.func
                        call_name: str | None = None
                        if isinstance(func, ast.Attribute):
                            call_name = func.attr
                        elif isinstance(func, ast.Name):
                            call_name = func.id
                        if call_name and call_name in _FORBIDDEN_DIRECT_CALLS and call_name not in seen:
                            seen.add(call_name)
                            violations.append(f"  - {sub.__name__}.{attr_name}() -> {call_name}()")

        def _check_subclasses(parent: type) -> None:
            for sub in parent.__subclasses__():
                if '_get_client' in sub.__dict__:
                    raise TypeError(f"{sub.__name__} must not override _get_client")
                ttl = getattr(sub, '__cache_ttl__', None)
                if not isinstance(ttl, int) or ttl <= 0:
                    raise ValueError(
                        f"{sub.__name__}.__cache_ttl__ must be a positive integer, got: {ttl!r}"
                    )
                _check_forbidden_calls(sub)
                _check_subclasses(sub)

        _check_subclasses(cls)

        if violations:
            nl = '\n'
            raise TypeError(
                f"The following subclass methods directly call cache invalidation methods "
                f"(may cause MissingGreenlet after commit):\n"
                f"{nl.join(violations)}\n"
                f"Use _register_pending_invalidation() + "
                f"_commit_and_invalidate()/_sync_invalidate_after_commit() instead."
            )

        # Register session event hooks (idempotent)
        cls._register_session_commit_hook()

    @classmethod
    def _register_session_commit_hook(cls) -> None:
        """
        Register SQLAlchemy Session after_commit/after_rollback event hooks.

        after_commit: automatically flushes pending invalidation types accumulated in session.info.
        Covers all commit paths: CRUD methods with commit=True, direct session.commit().

        after_rollback: clears accumulated pending types (data was rolled back, no invalidation needed).

        Idempotent: multiple calls register only once.

        Limitation (fire-and-forget):
        The after_commit handler is synchronous (SQLAlchemy event constraint), cannot await async
        invalidation. Uses ``loop.create_task()`` to schedule invalidation. There is an extremely
        short window between commit return and invalidation completion (typically < 1ms).
        CRUD methods with commit=True perform synchronous await invalidation (no window).
        This path only covers commit=False -> session.commit() scenarios.
        TTL provides eventual consistency fallback.
        """
        if cls._commit_hook_registered:
            return

        def _after_commit_handler(session: _SyncSession) -> None:
            pending: dict[type, set[Any]] | None = session.info.pop(_SESSION_PENDING_CACHE_KEY, None)
            if not pending:
                return
            loop = asyncio.get_running_loop()

            # Create sync-invalidation tracking dict for this commit cycle.
            # CRUD methods after super() returns (i.e. after this handler) write
            # (type, instance_ids) pairs. _compensate deduplicates by instance_id.
            synced: dict[type, set[Any]] = {}
            session.info[_SESSION_SYNCED_CACHE_KEY] = synced

            async def _compensate(
                    to_invalidate: dict[type, set[Any]],
                    already_synced: dict[type, set[Any]],
            ) -> None:
                sentinels = {_QUERY_ONLY_INVALIDATION, _FULL_MODEL_INVALIDATION}
                for model_type, pending_ids in to_invalidate.items():
                    if not issubclass(model_type, CachedTableBaseMixin):
                        continue

                    needs_full = _FULL_MODEL_INVALIDATION in pending_ids
                    synced_ids = already_synced.get(model_type)

                    if synced_ids is not None:
                        # Sync path already invalidated this type (query caches covered).
                        if needs_full and _FULL_MODEL_INVALIDATION not in synced_ids:
                            # pending has full sentinel but sync path didn't do full -- compensate
                            try:
                                await model_type._invalidate_for_model()
                            except Exception as e:
                                logger.error(
                                    "Post-commit compensating model-level invalidation failed (%s): %s",
                                    model_type.__name__, e,
                                )
                            continue
                        # Only compensate ID caches not handled by sync path
                        remaining = pending_ids - synced_ids - sentinels
                        if remaining:
                            try:
                                for _id in remaining:
                                    await model_type._invalidate_id_cache(_id)
                            except Exception as e:
                                logger.error(
                                    "Post-commit compensating ID cache invalidation failed (%s): %s",
                                    model_type.__name__, e,
                                )
                        continue

                    # Type was not handled by sync path at all -- full compensate
                    try:
                        if needs_full:
                            await model_type._invalidate_for_model()
                        else:
                            real_ids = pending_ids - sentinels
                            has_query_only = _QUERY_ONLY_INVALIDATION in pending_ids
                            if real_ids:
                                for _id in real_ids:
                                    await model_type._invalidate_id_cache(_id)
                                await model_type._invalidate_query_caches()
                            elif has_query_only:
                                await model_type._invalidate_query_caches()
                            else:
                                await model_type._invalidate_for_model()
                    except Exception as e:
                        logger.error(
                            "Post-commit compensating cache invalidation failed (%s): %s",
                            model_type.__name__, e,
                        )

            # fire-and-forget: instance_ids already handled by sync path are deduplicated;
            # only IDs accumulated via commit=False and not synced get compensated.
            _ = loop.create_task(_compensate(pending, synced))

        def _after_rollback_handler(session: _SyncSession) -> None:
            session.info.pop(_SESSION_PENDING_CACHE_KEY, None)
            session.info.pop(_SESSION_SYNCED_CACHE_KEY, None)

        event.listen(_SyncSession, "after_commit", _after_commit_handler)
        event.listen(_SyncSession, "after_rollback", _after_rollback_handler)

        cls._commit_hook_registered = True
        logger.debug("CachedTableBaseMixin: session commit/rollback event hooks registered")

    # ================================================================
    #  ID query detection
    # ================================================================

    @classmethod
    def _extract_id_from_condition(cls, condition: Any) -> Any | None:
        """
        Detect whether condition is a ``Model.id == value`` ID equality query.

        :returns: The ID value if it is an ID query, otherwise None.
        """
        if not isinstance(condition, BinaryExpression):
            return None
        if condition.operator is not operators.eq:
            return None
        left = condition.left
        if hasattr(left, 'key') and left.key == 'id':
            right = condition.right
            if hasattr(right, 'value'):
                return right.value
        return None

    @classmethod
    def _build_id_cache_key(cls, id_value: Any) -> str:
        """Build ID cache key. Format: ``id:{ModelName}:{id_value}``"""
        return f"{cls._ID_CACHE_KEY_PREFIX}:{cls.__name__}:{id_value}"

    # ================================================================
    #  load relation caching (multi-ID cache joint query)
    # ================================================================

    @classmethod
    def _has_pending_invalidation(cls, session: AsyncSession) -> bool:
        """Check whether session has pending cache invalidation related to cls."""
        pending: dict[type, set[Any]] | None = session.info.get(_SESSION_PENDING_CACHE_KEY)
        if not pending:
            return False
        return any(
            issubclass(pending_type, cls) or issubclass(cls, pending_type)
            for pending_type in pending
        )

    @classmethod
    def _analyze_load_relations(
            cls,
            load: QueryableAttribute[Any] | list[QueryableAttribute[Any]],
    ) -> 'list[tuple[str, type[CachedTableBaseMixin], str]] | None':
        """
        Analyze whether all load targets are MANYTOONE and cacheable.

        Only handles relationships directly belonging to cls (including inherited).
        Chained loading (e.g. Character.tool_set -> ToolSet.tools) is not supported.

        :returns: [(rel_name, target_cls, fk_attr_name), ...] or None if conditions not met.
        """
        load_list = load if isinstance(load, list) else [load]
        mapper = sa_inspect(cls)
        relationships = mapper.relationships  # pyright: ignore[reportOptionalMemberAccess]
        result: list[tuple[str, type[CachedTableBaseMixin], str]] = []

        for attr in load_list:
            # Check if this is a direct relationship of cls (including inherited)
            attr_owner: type[Any] = attr.class_  # pyright: ignore[reportAssignmentType]
            if not issubclass(cls, attr_owner):
                return None  # Chained loading, cannot handle from cache

            rel_name: str = attr.key
            if rel_name not in relationships:
                return None  # Not a relationship attribute

            rel_prop = relationships[rel_name]
            if rel_prop.direction is not MANYTOONE:
                return None  # Only MANYTOONE supported (FK is on the main model side)

            target_cls = rel_prop.mapper.class_
            if not issubclass(target_cls, CachedTableBaseMixin):
                return None  # Target model is not cacheable

            # Extract FK attribute name (e.g. permission_id)
            local_col = rel_prop.local_remote_pairs[0][0]
            fk_attr_name: str = local_col.key
            result.append((rel_name, target_cls, fk_attr_name))

        return result

    @classmethod
    async def _try_load_from_id_caches(
            cls,
            session: AsyncSession,
            id_value: Any,
            rel_info: 'list[tuple[str, type[CachedTableBaseMixin], str]]',
    ) -> Any:
        """
        Try to load main model + MANYTOONE relations from multiple ID caches.

        Flow:
        1. Look up main model ID cache -> deserialize
        2. For each relation in rel_info, look up related model's ID cache by FK value
        3. All hits -> merge into session + set_committed_value -> return
        4. Any miss -> return _LOAD_CACHE_MISS

        :returns: Model instance | None (cached empty result) | _LOAD_CACHE_MISS (need DB fallback)
        """
        # 1. Look up main model ID cache
        main_cache_key = cls._build_id_cache_key(id_value)
        main_raw = await cls._cache_get(main_cache_key)
        if main_raw is None:
            return _LOAD_CACHE_MISS

        try:
            main_obj = cls._deserialize_result(main_raw, 'first')
        except (ValidationError, Exception):
            try:
                await cls._cache_delete(main_cache_key)
            except Exception:
                pass
            return _LOAD_CACHE_MISS

        if main_obj is None:
            return None  # Cached empty result

        # 2. Look up each related model's ID cache
        rel_objects: list[tuple[str, Any]] = []
        for rel_name, target_cls, fk_attr_name in rel_info:
            fk_value = getattr(main_obj, fk_attr_name, None)
            if fk_value is None:
                # FK is NULL -> relation is None
                rel_objects.append((rel_name, None))
                continue

            # Check if the relation target model has pending uncommitted changes
            if target_cls._has_pending_invalidation(session):
                return _LOAD_CACHE_MISS

            rel_cache_key = target_cls._build_id_cache_key(fk_value)
            rel_raw = await target_cls._cache_get(rel_cache_key)
            if rel_raw is None:
                return _LOAD_CACHE_MISS

            try:
                rel_obj = target_cls._deserialize_result(rel_raw, 'first')
            except (ValidationError, Exception):
                try:
                    await target_cls._cache_delete(rel_cache_key)
                except Exception:
                    pass
                return _LOAD_CACHE_MISS

            rel_objects.append((rel_name, rel_obj))

        # 3. All hits -> merge into session identity map
        # Merge related objects first
        merged_rels: list[tuple[str, Any]] = []
        for rel_name, rel_obj in rel_objects:
            if rel_obj is not None:
                make_transient_to_detached(rel_obj)
                rel_obj = await session.merge(rel_obj, load=False)
            merged_rels.append((rel_name, rel_obj))

        # Merge main object
        make_transient_to_detached(main_obj)
        main_obj = await session.merge(main_obj, load=False)

        # Set relationship attributes (without triggering ORM change tracking)
        for rel_name, rel_obj in merged_rels:
            set_committed_value(main_obj, rel_name, rel_obj)

        return main_obj

    @classmethod
    async def _write_load_result_to_id_caches(
            cls,
            result: Any,
            rel_info: 'list[tuple[str, type[CachedTableBaseMixin], str]]',
    ) -> None:
        """
        After a DB query with load, write main model and MANYTOONE relation models
        into their respective ID caches.

        Main model and relation models are cached independently (each with their own
        ID cache key + TTL), and invalidated independently.
        """
        if result is None:
            return

        items = [result] if not isinstance(result, list) else result

        for item in items:
            if item is None:
                continue

            # Write main model to cls's ID cache
            item_id = getattr(item, 'id', None)
            if item_id is not None:
                try:
                    cache_key = cls._build_id_cache_key(item_id)
                    serialized = cls._serialize_result(item)
                    await cls._cache_set(cache_key, serialized, cls.__cache_ttl__)
                except Exception as e:
                    logger.error("Cache main model write failed (%s:%s): %s", cls.__name__, item_id, e)

            # Write relation models to their target_cls ID caches
            for rel_name, target_cls, _fk_attr in rel_info:
                rel_obj = getattr(item, rel_name, None)
                if rel_obj is None:
                    continue
                rel_id = getattr(rel_obj, 'id', None)
                if rel_id is None:
                    continue
                actual_rel_cls = type(rel_obj)
                if not issubclass(actual_rel_cls, CachedTableBaseMixin):
                    continue
                try:
                    cache_key = target_cls._build_id_cache_key(rel_id)
                    serialized = target_cls._serialize_result(rel_obj)
                    await target_cls._cache_set(cache_key, serialized, target_cls.__cache_ttl__)
                except Exception as e:
                    logger.error(
                        "Cache relation model write failed (%s:%s): %s",
                        target_cls.__name__, rel_id, e,
                    )

    # ================================================================
    #  Cache key construction
    # ================================================================

    @classmethod
    def _build_cache_key(
            cls,
            condition: Any,
            fetch_mode: str,
            offset: int | None,
            limit: int | None,
            order_by: Any,
            load: Any,
            filter_expr: Any,
            table_view: Any,
            *time_args: Any,
    ) -> str:
        """
        Build a deterministic cache key from query parameters.

        Merges table_view into explicit parameters (mirrors table.py merge logic)
        so that semantically identical queries produce the same key regardless
        of whether parameters come from table_view or explicit args.

        time_args order: created_before, created_after, updated_before, updated_after

        Format: ``{_CACHE_KEY_PREFIX}:{ModelName}:{md5_hash[:_CACHE_KEY_HASH_LENGTH]}``
        """
        # ---- Normalize: merge table_view into explicit parameters ----
        merged_times = list(time_args)
        if table_view is not None:
            tv_times = [
                table_view.created_before_datetime,
                table_view.created_after_datetime,
                table_view.updated_before_datetime,
                table_view.updated_after_datetime,
            ]
            for i in range(min(len(merged_times), 4)):
                if merged_times[i] is None:
                    merged_times[i] = tv_times[i]
            if offset is None:
                offset = table_view.offset
            if limit is None:
                limit = table_view.limit
            if order_by is None:
                parts_order = f"ob={table_view.order},{'d' if table_view.desc else 'a'}"
            else:
                parts_order = None
        else:
            parts_order = None

        parts: list[str] = [fetch_mode]

        # condition -> SQL string (attempt dialect compilation, fallback to repr)
        if condition is None:
            parts.append("none")
        elif isinstance(condition, bool):
            parts.append(str(condition))
        else:
            try:
                # Try PostgreSQL dialect first (most common for production)
                from sqlalchemy.dialects import postgresql
                compiled = condition.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
                parts.append(str(compiled))
            except Exception:
                # Fallback: use default compilation or repr
                try:
                    parts.append(str(condition.compile(compile_kwargs={"literal_binds": True})))
                except Exception:
                    parts.append(repr(condition))

        # Pagination (already normalized)
        if offset is not None:
            parts.append(f"o={offset}")
        if limit is not None:
            parts.append(f"l={limit}")

        # Sort order (already normalized)
        if order_by:
            for ob in order_by:
                try:
                    parts.append(str(ob.compile()))
                except Exception:
                    parts.append(repr(ob))
        elif parts_order is not None:
            parts.append(parts_order)

        # load parameter (affects returned data content)
        if load is not None:
            load_list = load if isinstance(load, list) else [load]
            parts.append("load=" + ",".join(str(item.key) for item in load_list))

        # filter (bool values also need to be in key: False = WHERE FALSE, True = unconditional)
        if filter_expr is not None:
            if isinstance(filter_expr, bool):
                parts.append(f"f={filter_expr}")
            else:
                try:
                    parts.append("f=" + str(filter_expr.compile(
                        compile_kwargs={"literal_binds": True},
                    )))
                except Exception:
                    parts.append("f=" + repr(filter_expr))

        # Time filtering (already normalized; table_view time fields merged)
        for i, ta in enumerate(merged_times):
            if ta is not None:
                parts.append(f"t{i}={ta.isoformat()}")

        key_hash = hashlib.md5("|".join(parts).encode()).hexdigest()[:cls._CACHE_KEY_HASH_LENGTH]
        return f"{cls._CACHE_KEY_PREFIX}:{cls.__name__}:{key_hash}"

    # ================================================================
    #  Serialization / Deserialization
    # ================================================================

    @classmethod
    def _serialize_item(cls, item: Any) -> bytes:
        """
        Serialize a single SQLModelBase instance to bytes, including the actual class name.

        In polymorphic scenarios (STI), querying the base class may return subclass instances.
        The actual class name is recorded to ensure correct subclass restoration on deserialization.

        Only serializes column fields (column attrs), excludes:
        - Relationship attributes (lazy='raise_on_sql' would error; ID cache only stores column data)
        - computed_field (may depend on relationships, e.g. ToolSet.tool_count -> self.tools)

        For load queries, main model and relation models are independently serialized
        into their respective ID caches.
        """
        if isinstance(item, SQLModelBase):
            mapper = sa_inspect(type(item))
            column_fields: set[str] = {prop.key for prop in mapper.column_attrs}
            item_dict: dict[str, Any] = item.model_dump(mode='json', include=column_fields)
            item_dict[_WRAPPER_CLASS_KEY] = type(item).__name__
            return _json_dumps(item_dict)
        return _json_dumps(item)

    @classmethod
    def _serialize_result(cls, result: Any) -> bytes:
        """
        Serialize a get() query result to bytes (for Redis storage).

        Uses JSON serialization + wrapper format to distinguish None/single/list.
        Each SQLModelBase item includes a ``_c`` field recording the actual class name (polymorphic-safe).
        """
        if result is None:
            return _json_dumps({_WRAPPER_TYPE_KEY: _CacheResultType.NONE})
        elif isinstance(result, list):
            serialized_items = [cls._serialize_item(item).decode('utf-8') for item in result]
            items_json = "[" + ",".join(serialized_items) + "]"
            wrapper = (
                f'{{"{_WRAPPER_TYPE_KEY}":"{_CacheResultType.LIST}"'
                f',"{_WRAPPER_ITEMS_KEY}":{items_json}}}'
            )
            return wrapper.encode('utf-8')
        else:
            data_json = cls._serialize_item(result).decode('utf-8')
            wrapper = (
                f'{{"{_WRAPPER_TYPE_KEY}":"{_CacheResultType.SINGLE}"'
                f',"{_WRAPPER_DATA_KEY}":{data_json}}}'
            )
            return wrapper.encode('utf-8')

    @classmethod
    def _resolve_subclass(cls, class_name: str | None) -> type:
        """
        Resolve actual subclass from class name. Used for polymorphic deserialization.

        Uses ``_subclass_name_cache`` to cache lookup results; O(1) after first recursive traversal.
        Cache key includes the query starting class name to distinguish different inheritance trees.
        """
        if class_name is None or cls.__name__ == class_name:
            return cls

        lookup_key = f"{cls.__name__}.{class_name}"
        cached = CachedTableBaseMixin._subclass_name_cache.get(lookup_key)
        if cached is not None:
            return cached

        def _walk(klass: type) -> type | None:
            for sub in klass.__subclasses__():
                if sub.__name__ == class_name:
                    return sub
                found = _walk(sub)
                if found is not None:
                    return found
            return None

        resolved = _walk(cls) or cls
        CachedTableBaseMixin._subclass_name_cache[lookup_key] = resolved
        return resolved

    @classmethod
    def _deserialize_item(cls, item_data: dict[str, Any]) -> Any:
        """
        Reconstruct a single model instance from cached dict.

        Reads the ``_c`` field to resolve the actual subclass (polymorphic-safe),
        then pops ``_c`` and calls ``model_validate``.
        """
        class_name = item_data.pop(_WRAPPER_CLASS_KEY, None)
        actual_cls = cls._resolve_subclass(class_name)
        return actual_cls.model_validate(item_data)

    @classmethod
    def _deserialize_result(cls, raw: bytes, _fetch_mode: str) -> Any:
        """
        Reconstruct a get() query result from cached bytes.

        Uses ``json.loads`` -> ``model_validate`` (not ``model_validate_json``,
        because ``model_validate_json`` returns str for UUID fields on table=True models).

        :raises ValidationError: Schema mismatch
        :raises json.JSONDecodeError: Invalid JSON
        """
        cached = _json_loads(raw)
        result_type = cached.get(_WRAPPER_TYPE_KEY)
        if result_type == _CacheResultType.NONE:
            return None
        elif result_type == _CacheResultType.LIST:
            return [cls._deserialize_item(item) for item in cached[_WRAPPER_ITEMS_KEY]]
        elif result_type == _CacheResultType.SINGLE:
            return cls._deserialize_item(cached[_WRAPPER_DATA_KEY])
        raise ValueError(
            f"Unknown cache result type: {result_type!r}, "
            f"expected {_CacheResultType.NONE!r}/{_CacheResultType.LIST!r}/{_CacheResultType.SINGLE!r}"
        )

    # ================================================================
    #  Invalidation
    # ================================================================

    @classmethod
    async def _invalidate_id_cache(cls, instance_id: Any) -> None:
        """
        Row-level DEL: delete ID cache for the given ID + ancestor ID caches.

        O(1) operation, does not use SCAN.
        """
        prefix = cls._ID_CACHE_KEY_PREFIX
        await cls._cache_delete(f"{prefix}:{cls.__name__}:{instance_id}")
        for ancestor in cls.__mro__:
            if ancestor is cls or ancestor is object:
                continue
            if (
                issubclass(ancestor, CachedTableBaseMixin)
                and ancestor is not CachedTableBaseMixin
            ):
                await cls._cache_delete(f"{prefix}:{ancestor.__name__}:{instance_id}")

    @classmethod
    async def _invalidate_query_caches(cls) -> None:
        """Model-level SCAN+DEL: delete all query caches + ancestor query caches."""
        prefix = cls._CACHE_KEY_PREFIX
        await cls._cache_delete_pattern(f"{prefix}:{cls.__name__}:*")
        for ancestor in cls.__mro__:
            if ancestor is cls or ancestor is object:
                continue
            if (
                issubclass(ancestor, CachedTableBaseMixin)
                and ancestor is not CachedTableBaseMixin
            ):
                await cls._cache_delete_pattern(f"{prefix}:{ancestor.__name__}:*")

    @classmethod
    async def _invalidate_for_model(cls, _instance_id: Any | None = None) -> None:
        """
        Invalidate caches: ID cache + query cache.

        Called after save/update/delete.

        - ``_instance_id`` provided: row-level DEL that ID's cache
        - ``_instance_id`` is None: model-level SCAN+DEL all ID caches
        - Query caches always model-level SCAN+DEL
        """
        if _instance_id is not None:
            await cls._invalidate_id_cache(_instance_id)
        else:
            # No instance_id -> model-level clear all ID caches
            id_prefix = cls._ID_CACHE_KEY_PREFIX
            await cls._cache_delete_pattern(f"{id_prefix}:{cls.__name__}:*")
            for ancestor in cls.__mro__:
                if ancestor is cls or ancestor is object:
                    continue
                if (
                    issubclass(ancestor, CachedTableBaseMixin)
                    and ancestor is not CachedTableBaseMixin
                ):
                    await cls._cache_delete_pattern(f"{id_prefix}:{ancestor.__name__}:*")
        # Query caches always invalidated at model level
        await cls._invalidate_query_caches()

    @classmethod
    async def invalidate_by_id(cls, *_ids: Any) -> None:
        """
        Public API: manually invalidate caches for specific IDs.

        For external use only (admin scripts, tests, non-model code).
        Model internal raw SQL methods should use ``_register_pending_invalidation()`` +
        ``_commit_and_invalidate()`` / ``_sync_invalidate_after_commit()``
        to avoid MissingGreenlet from accessing expired attributes after commit.

        Each ID gets row-level DEL on id cache; query caches get model-level SCAN+DEL.

        Redis errors are logged and swallowed (fire-and-forget) to avoid
        DB-committed-but-500-returned inconsistency.
        """
        try:
            for _id in _ids:
                await cls._invalidate_id_cache(_id)
            await cls._invalidate_query_caches()
        except Exception as e:
            logger.error("invalidate_by_id() Redis invalidation failed (%s, ids=%s): %s", cls.__name__, _ids, e)

    @classmethod
    async def invalidate_all(cls) -> None:
        """
        Public API: invalidate all caches for this model (id + query).

        Redis errors are logged and swallowed (same as invalidate_by_id).
        """
        try:
            await cls._invalidate_for_model()
        except Exception as e:
            logger.error("invalidate_all() Redis invalidation failed (%s): %s", cls.__name__, e)

    # ================================================================
    #  get() override -- cache read path
    # ================================================================

    @overload
    @classmethod
    async def get(
            cls,
            session: AsyncSession,
            condition: ColumnElement[bool] | bool | None = None,
            *,
            offset: int | None = None,
            limit: int | None = None,
            fetch_mode: Literal["all"],
            join: type[TableBaseMixin] | tuple[type[TableBaseMixin], _OnClauseArgument] | None = None,
            options: list[ExecutableOption] | None = None,
            load: QueryableAttribute[Any] | list[QueryableAttribute[Any]] | None = None,
            order_by: list[ColumnElement[Any]] | None = None,
            filter: ColumnElement[bool] | bool | None = None,
            with_for_update: bool = False,
            table_view: TableViewRequest | None = None,
            jti_subclasses: list[type[PolymorphicBaseMixin]] | Literal['all'] | None = None,
            populate_existing: bool = False,
            no_cache: bool = False,
            created_before_datetime: datetime | None = None,
            created_after_datetime: datetime | None = None,
            updated_before_datetime: datetime | None = None,
            updated_after_datetime: datetime | None = None,
    ) -> list[Self]: ...

    @overload
    @classmethod
    async def get(
            cls,
            session: AsyncSession,
            condition: ColumnElement[bool] | bool | None = None,
            *,
            offset: int | None = None,
            limit: int | None = None,
            fetch_mode: Literal["one"],
            join: type[TableBaseMixin] | tuple[type[TableBaseMixin], _OnClauseArgument] | None = None,
            options: list[ExecutableOption] | None = None,
            load: QueryableAttribute[Any] | list[QueryableAttribute[Any]] | None = None,
            order_by: list[ColumnElement[Any]] | None = None,
            filter: ColumnElement[bool] | bool | None = None,
            with_for_update: bool = False,
            table_view: TableViewRequest | None = None,
            jti_subclasses: list[type[PolymorphicBaseMixin]] | Literal['all'] | None = None,
            populate_existing: bool = False,
            no_cache: bool = False,
            created_before_datetime: datetime | None = None,
            created_after_datetime: datetime | None = None,
            updated_before_datetime: datetime | None = None,
            updated_after_datetime: datetime | None = None,
    ) -> Self: ...

    @overload
    @classmethod
    async def get(
            cls,
            session: AsyncSession,
            condition: ColumnElement[bool] | bool | None = None,
            *,
            offset: int | None = None,
            limit: int | None = None,
            fetch_mode: Literal["first"] = ...,
            join: type[TableBaseMixin] | tuple[type[TableBaseMixin], _OnClauseArgument] | None = None,
            options: list[ExecutableOption] | None = None,
            load: QueryableAttribute[Any] | list[QueryableAttribute[Any]] | None = None,
            order_by: list[ColumnElement[Any]] | None = None,
            filter: ColumnElement[bool] | bool | None = None,
            with_for_update: bool = False,
            table_view: TableViewRequest | None = None,
            jti_subclasses: list[type[PolymorphicBaseMixin]] | Literal['all'] | None = None,
            populate_existing: bool = False,
            no_cache: bool = False,
            created_before_datetime: datetime | None = None,
            created_after_datetime: datetime | None = None,
            updated_before_datetime: datetime | None = None,
            updated_after_datetime: datetime | None = None,
    ) -> Self | None: ...

    @classmethod  # @override -- MRO runtime override of TableBaseMixin.get()
    async def get(
            cls,
            session: AsyncSession,
            condition: ColumnElement[bool] | bool | None = None,
            *,
            offset: int | None = None,
            limit: int | None = None,
            fetch_mode: Literal["one", "first", "all"] = "first",
            join: type[TableBaseMixin] | tuple[type[TableBaseMixin], _OnClauseArgument] | None = None,
            options: list[ExecutableOption] | None = None,
            load: QueryableAttribute[Any] | list[QueryableAttribute[Any]] | None = None,
            order_by: list[ColumnElement[Any]] | None = None,
            filter: ColumnElement[bool] | bool | None = None,
            with_for_update: bool = False,
            table_view: TableViewRequest | None = None,
            jti_subclasses: list[type[PolymorphicBaseMixin]] | Literal['all'] | None = None,
            populate_existing: bool = False,
            no_cache: bool = False,
            created_before_datetime: datetime | None = None,
            created_after_datetime: datetime | None = None,
            updated_before_datetime: datetime | None = None,
            updated_after_datetime: datetime | None = None,
    ) -> list[Self] | Self | None:
        """
        Cached get() -- intercepts TableBaseMixin.get(), returns from cache on hit.

        - ``no_cache`` only exists on this mixin; passing it to a non-cached model raises TypeError.
        - Within a transaction with commit=False and uncommitted changes, cache is automatically skipped.
        - Cache-hit objects are merged into the session identity map via ``session.merge(load=False)``.
        - When ``load`` specifies cacheable MANYTOONE relations, attempts multi-ID cache joint query (zero SQL).
        - See module docstring for full cache skip conditions.
        """
        skip_cache = (
            no_cache
            or options is not None
            or with_for_update
            or populate_existing
            or join is not None
        )

        # Uncommitted changes in transaction -> skip cache (both read and write)
        if not skip_cache and cls._has_pending_invalidation(session):
            skip_cache = True

        # ---- load query: multi-ID cache joint optimization ----
        # MANYTOONE + cacheable target -> look up main model and relation models' ID caches separately
        if load is not None:
            rel_info: list[tuple[str, type[CachedTableBaseMixin], str]] | None = None
            if not skip_cache and jti_subclasses is None:
                rel_info = cls._analyze_load_relations(load)
                if rel_info is not None:
                    # Detect simple ID query (multi-ID cache only supports single-row ID equality)
                    id_value = cls._extract_id_from_condition(condition)
                    is_simple = (
                        id_value is not None
                        and fetch_mode in ('first', 'one')
                        and offset is None and limit is None
                        and order_by is None and filter is None
                        and table_view is None
                        and created_before_datetime is None and created_after_datetime is None
                        and updated_before_datetime is None and updated_after_datetime is None
                    )
                    if is_simple:
                        cached = await cls._try_load_from_id_caches(session, id_value, rel_info)
                        if cached is not _LOAD_CACHE_MISS:
                            return cached

            # Cache miss / not simple query / not optimizable -> DB query
            result = await super().get(
                session, condition,
                offset=offset, limit=limit, fetch_mode=fetch_mode,
                join=join, options=options, load=load,
                order_by=order_by, filter=filter,
                with_for_update=with_for_update, table_view=table_view,
                jti_subclasses=jti_subclasses,
                populate_existing=populate_existing,
                created_before_datetime=created_before_datetime,
                created_after_datetime=created_after_datetime,
                updated_before_datetime=updated_before_datetime,
                updated_after_datetime=updated_after_datetime,
            )

            # Write main model + relation models to their ID caches (only for optimizable scenarios)
            if rel_info is not None and not skip_cache:
                await cls._write_load_result_to_id_caches(result, rel_info)

            return result

        # ---- Non-load query (original logic) ----
        cache_key: str | None = None
        if not skip_cache:
            # Detect pure ID equality query (no pagination/sort/time filters)
            id_value = cls._extract_id_from_condition(condition)
            is_simple_id_query = (
                id_value is not None
                and fetch_mode in ('first', 'one')
                and offset is None and limit is None
                and order_by is None and filter is None
                and table_view is None
                and created_before_datetime is None and created_after_datetime is None
                and updated_before_datetime is None and updated_after_datetime is None
            )

            if is_simple_id_query:
                cache_key = cls._build_id_cache_key(id_value)
            else:
                cache_key = cls._build_cache_key(
                    condition, fetch_mode, offset, limit,
                    order_by, load, filter, table_view,
                    created_before_datetime, created_after_datetime,
                    updated_before_datetime, updated_after_datetime,
                )

            raw = await cls._cache_get(cache_key)
            if raw is not None:
                # Phase 1: Deserialize -- failure means corrupted cache (schema change etc.)
                try:
                    result = cls._deserialize_result(raw, fetch_mode)
                except (ValidationError, Exception) as e:
                    # Bad cache: try to delete and fall back to DB
                    logger.warning(
                        "Cache deserialization failed, deleting bad key and falling back to DB: "
                        "%s (%s: %s)", cache_key, type(e).__name__, e,
                    )
                    try:
                        await cls._cache_delete(cache_key)
                    except Exception as del_err:
                        logger.error("Bad cache cleanup failed key='%s': %s", cache_key, del_err)
                    # fall through to DB query below
                else:
                    # Phase 2: Merge into session identity map (consistent with DB query semantics)
                    if result is not None:
                        if isinstance(result, list):
                            merged_list: list[Self] = []
                            for item in result:
                                make_transient_to_detached(item)
                                merged_list.append(await session.merge(item, load=False))
                            return merged_list
                        else:
                            make_transient_to_detached(result)
                            result = await session.merge(result, load=False)
                    return result

        # DB query (via MRO calls TableBaseMixin.get())
        # Note: do not forward no_cache -- TableBaseMixin.get() does not accept this parameter
        result = await super().get(
            session, condition,
            offset=offset, limit=limit, fetch_mode=fetch_mode,
            join=join, options=options, load=load,
            order_by=order_by, filter=filter,
            with_for_update=with_for_update, table_view=table_view,
            jti_subclasses=jti_subclasses,
            populate_existing=populate_existing,
            created_before_datetime=created_before_datetime,
            created_after_datetime=created_after_datetime,
            updated_before_datetime=updated_before_datetime,
            updated_after_datetime=updated_after_datetime,
        )

        # Write to cache (only when not skipping)
        if not skip_cache and cache_key is not None:
            try:
                serialized = cls._serialize_result(result)
                await cls._cache_set(cache_key, serialized, cls.__cache_ttl__)
            except Exception as e:
                logger.error("Cache serialization/write failed: %s: %s", type(e).__name__, e)

        return result

    # ================================================================
    #  Deferred commit compensation (session.info tracking + after_commit event)
    # ================================================================

    @staticmethod
    def _register_pending_invalidation(
            session: AsyncSession,
            model_type: type,
            instance_id: Any | None = None,
    ) -> None:
        """
        Record model type (and optional instance_id) in session.info for commit-time compensating invalidation.

        pending structure: ``dict[type, set[Any]]``
        - key: model type
        - value: set of pending invalidation instance_ids for that type

        Sentinel semantics:
        - ``_FULL_MODEL_INVALIDATION``: condition delete, needs model-level full invalidation (highest priority)
        - ``_QUERY_ONLY_INVALIDATION``: add() scenario, only invalidate query caches
        - Normal UUID/int: row-level ID invalidation

        Carrying instance_id enables the after_commit compensation path to do row-level
        invalidation, avoiding model-level SCAN+DEL that would delete other rows' ID caches.
        """
        pending: dict[type, set[Any]] = session.info.setdefault(_SESSION_PENDING_CACHE_KEY, {})
        ids = pending.setdefault(model_type, set())
        if instance_id is not None:
            ids.add(instance_id)

    # ================================================================
    #  save() override -- write-through invalidation
    # ================================================================

    async def save(
            self,
            session: AsyncSession,
            load: QueryableAttribute[Any] | list[QueryableAttribute[Any]] | None = None,
            refresh: bool = True,
            commit: bool = True,
            jti_subclasses: list[type[PolymorphicBaseMixin]] | Literal['all'] | None = None,
            optimistic_retry_count: int = 0,
    ) -> Self:  # MRO override TableBaseMixin.save()
        """
        save() with cache invalidation, then refresh via get() (ensures get() won't hit stale cache).

        Flow (commit=True, refresh=True):
        1. super().save(refresh=False) -- only commit, no refresh
        2. Synchronous cache invalidation -- ensures old data removed from Redis
        3. get() refresh -- cache invalidated, get() queries DB and backfills cache

        New objects (id is None): register _QUERY_ONLY_INVALIDATION (no old ID cache possible),
        after save, use result's DB-generated id for sync path marking.
        """
        model_type = type(self)
        # New object id may be None (DB-generated); only need to invalidate query caches
        instance_id = getattr(self, 'id', None)
        if instance_id is not None:
            self._register_pending_invalidation(session, model_type, instance_id)
        else:
            self._register_pending_invalidation(session, model_type, _QUERY_ONLY_INVALIDATION)

        # refresh=False: skip super()'s internal get(), this method refreshes after invalidation
        result = await super().save(
            session,
            refresh=False,
            commit=commit,
            optimistic_retry_count=optimistic_retry_count,
        )

        # After super().save(refresh=False), commit expires the object.
        # Direct getattr(result, 'id') would trigger synchronous lazy load -> MissingGreenlet.
        # Use sa_inspect to safely read id from identity map (no DB query).
        _insp = cast(InstanceState[Any], sa_inspect(result))
        if _insp.identity:
            instance_id = _insp.identity[0]

        # Invalidate cache first (ensures subsequent get() won't hit stale data)
        pending = session.info.get(_SESSION_PENDING_CACHE_KEY)
        if not pending or model_type not in pending:
            try:
                if instance_id is not None:
                    await model_type._invalidate_id_cache(instance_id)
                    await model_type._invalidate_query_caches()
                else:
                    await model_type._invalidate_query_caches()
            except Exception as e:
                logger.error("save() sync cache invalidation failed (%s): %s", model_type.__name__, e)
            else:
                synced = session.info.get(_SESSION_SYNCED_CACHE_KEY)
                if isinstance(synced, dict):
                    synced.setdefault(model_type, set()).add(
                        instance_id if instance_id is not None else _QUERY_ONLY_INVALIDATION
                    )

        # Write-through refresh: bypass cache read -> query DB -> proactively backfill ID cache.
        # Bypass read: avoid reading stale cache in partial invalidation scenarios.
        # Proactive backfill: maintain high cache hit rate; next external get() hits fresh data.
        # Only backfill on commit=True: commit=False data may be rolled back, cannot write to cache.
        if refresh:
            assert instance_id is not None, f"{model_type.__name__} id is None after save"
            result = await model_type.get(
                session, model_type.id == instance_id,
                load=load, jti_subclasses=jti_subclasses,
                no_cache=True,
            )
            assert result is not None, f"{model_type.__name__} record not found (id={instance_id})"

            # Proactive ID cache backfill (commit=True + no load)
            if commit and load is None:
                try:
                    cache_key = model_type._build_id_cache_key(instance_id)
                    serialized = model_type._serialize_result(result)
                    await model_type._cache_set(cache_key, serialized, model_type.__cache_ttl__)
                except Exception as e:
                    logger.error("save() cache backfill failed (%s): %s", model_type.__name__, e)

        return result

    # ================================================================
    #  update() override -- write-through invalidation
    # ================================================================

    async def update(
            self,
            session: AsyncSession,
            other: SQLModelBase,
            extra_data: dict[str, Any] | None = None,
            exclude_unset: bool = True,
            exclude: set[str] | None = None,
            load: QueryableAttribute[Any] | list[QueryableAttribute[Any]] | None = None,
            refresh: bool = True,
            commit: bool = True,
            jti_subclasses: list[type[PolymorphicBaseMixin]] | Literal['all'] | None = None,
            optimistic_retry_count: int = 0,
    ) -> Self:  # MRO override
        """update() with cache invalidation, then refresh via get(). Same logic as save()."""
        model_type = type(self)
        instance_id = getattr(self, 'id', None)
        self._register_pending_invalidation(session, model_type, instance_id)

        # refresh=False: skip super()'s internal get(), this method refreshes after invalidation
        result = await super().update(
            session, other,
            extra_data=extra_data,
            exclude_unset=exclude_unset,
            exclude=exclude,
            refresh=False,
            commit=commit,
            optimistic_retry_count=optimistic_retry_count,
        )

        # Invalidate cache first
        pending = session.info.get(_SESSION_PENDING_CACHE_KEY)
        if not pending or model_type not in pending:
            try:
                await model_type._invalidate_for_model(instance_id)
            except Exception as e:
                logger.error("update() sync cache invalidation failed (%s): %s", model_type.__name__, e)
            else:
                synced = session.info.get(_SESSION_SYNCED_CACHE_KEY)
                if isinstance(synced, dict):
                    synced.setdefault(model_type, set()).add(instance_id)

        # Write-through refresh: bypass cache read -> query DB -> proactively backfill ID cache
        if refresh:
            assert instance_id is not None, f"{model_type.__name__} id is None after update"
            result = await model_type.get(
                session, model_type.id == instance_id,
                load=load, jti_subclasses=jti_subclasses,
                no_cache=True,
            )
            assert result is not None, f"{model_type.__name__} record not found (id={instance_id})"

            # Proactive ID cache backfill (commit=True + no load)
            if commit and load is None:
                try:
                    cache_key = model_type._build_id_cache_key(instance_id)
                    serialized = model_type._serialize_result(result)
                    await model_type._cache_set(cache_key, serialized, model_type.__cache_ttl__)
                except Exception as e:
                    logger.error("update() cache backfill failed (%s): %s", model_type.__name__, e)

        return result

    # ================================================================
    #  delete() override -- post-delete invalidation
    # ================================================================

    @classmethod  # MRO override TableBaseMixin.delete()
    async def delete(
            cls,
            session: AsyncSession,
            instances: Self | list[Self] | None = None,
            *,
            condition: ColumnElement[bool] | bool | None = None,
            commit: bool = True,
    ) -> int:
        """
        delete() with cache invalidation.

        - instances provided: row-level DEL per instance's id cache + model-level query cache
        - condition or no args: model-level SCAN+DEL (id + query)
        """
        # Extract instance IDs (before super().delete(), objects may be inaccessible after delete)
        instance_ids: list[Any] = []
        if instances is not None:
            _instances = instances if isinstance(instances, list) else [instances]
            for inst in _instances:
                _id = getattr(inst, 'id', None)
                if _id is not None:
                    instance_ids.append(_id)

        # Register pending with instance_ids so compensation path can do row-level invalidation
        for _id in instance_ids:
            cls._register_pending_invalidation(session, cls, _id)
        if not instance_ids:
            # Condition delete cannot extract specific IDs; use sentinel for model-level full invalidation
            cls._register_pending_invalidation(session, cls, _FULL_MODEL_INVALIDATION)
        result = await super().delete(session, instances, condition=condition, commit=commit)
        pending = session.info.get(_SESSION_PENDING_CACHE_KEY)
        if not pending or cls not in pending:
            try:
                if instance_ids:
                    for _id in instance_ids:
                        await cls._invalidate_id_cache(_id)
                    await cls._invalidate_query_caches()
                else:
                    await cls._invalidate_for_model()
            except Exception as e:
                logger.error("delete() sync cache invalidation failed (%s): %s", cls.__name__, e)
            else:
                synced = session.info.get(_SESSION_SYNCED_CACHE_KEY)
                if isinstance(synced, dict):
                    if instance_ids:
                        synced.setdefault(cls, set()).update(instance_ids)
                    else:
                        synced.setdefault(cls, set()).add(_FULL_MODEL_INVALIDATION)
        return result

    # ================================================================
    #  add() override -- write-through invalidation
    # ================================================================

    @classmethod  # MRO override TableBaseMixin.add()
    async def add(
            cls,
            session: AsyncSession,
            instances: Self | list[Self],
            refresh: bool = True,
            commit: bool = True,
    ) -> Self | list[Self]:
        """
        add() with cache invalidation, then refresh via get().

        Always invalidates query caches (list queries may need to include new items).
        Instances with explicit IDs also invalidate their id caches (prevents stale cache on ID reuse).
        """
        # Collect explicit IDs (manually passed by caller, not auto-generated by default_factory)
        # model_fields_set only includes fields explicitly passed at construction time
        items = instances if isinstance(instances, list) else [instances]
        explicit_ids: list[Any] = [
            _id for item in items
            if isinstance(item, SQLModelBase) and 'id' in item.model_fields_set
            and (_id := getattr(item, 'id', None)) is not None
        ]

        cls._register_pending_invalidation(session, cls, _QUERY_ONLY_INVALIDATION)
        for _id in explicit_ids:
            cls._register_pending_invalidation(session, cls, _id)

        # refresh=False: skip super()'s internal refresh, this method refreshes after invalidation
        result = await super().add(session, instances, refresh=False, commit=commit)

        # Invalidate cache first
        pending = session.info.get(_SESSION_PENDING_CACHE_KEY)
        if not pending or cls not in pending:
            try:
                for _id in explicit_ids:
                    await cls._invalidate_id_cache(_id)
                await cls._invalidate_query_caches()
            except Exception as e:
                logger.error("add() sync cache invalidation failed (%s): %s", cls.__name__, e)
            else:
                synced = session.info.get(_SESSION_SYNCED_CACHE_KEY)
                if isinstance(synced, dict):
                    s = synced.setdefault(cls, set())
                    s.add(_QUERY_ONLY_INVALIDATION)
                    s.update(explicit_ids)

        # Write-through refresh: bypass cache read -> query DB -> proactively backfill ID cache
        # Only backfill on commit=True: commit=False data may be rolled back.
        if refresh:
            if isinstance(result, list):
                refreshed: list[Self] = []
                for inst in result:
                    # After commit, objects expire; use sa_inspect to safely read id
                    _insp = cast(InstanceState[Any], sa_inspect(inst))
                    _inst_id = _insp.identity[0] if _insp.identity else None
                    assert _inst_id is not None, f"{cls.__name__} id is None after add"
                    r = await cls.get(session, cls.id == _inst_id, no_cache=True)
                    assert r is not None, f"{cls.__name__} record not found (id={_inst_id})"
                    if commit:
                        try:
                            cache_key = cls._build_id_cache_key(_inst_id)
                            serialized = cls._serialize_result(r)
                            await cls._cache_set(cache_key, serialized, cls.__cache_ttl__)
                        except Exception as e:
                            logger.error("add() cache backfill failed (%s): %s", cls.__name__, e)
                    refreshed.append(r)
                return refreshed
            else:
                _insp = cast(InstanceState[Any], sa_inspect(result))
                _result_id = _insp.identity[0] if _insp.identity else None
                assert _result_id is not None, f"{cls.__name__} id is None after add"
                r = await cls.get(session, cls.id == _result_id, no_cache=True)
                assert r is not None, f"{cls.__name__} record not found (id={_result_id})"
                if commit:
                    try:
                        cache_key = cls._build_id_cache_key(_result_id)
                        serialized = cls._serialize_result(r)
                        await cls._cache_set(cache_key, serialized, cls.__cache_ttl__)
                    except Exception as e:
                        logger.error("add() cache backfill failed (%s): %s", cls.__name__, e)
                return r

        return result

    # ================================================================
    #  Raw SQL helpers -- commit + synchronous invalidation
    # ================================================================

    async def _commit_and_invalidate(self, session: AsyncSession) -> None:
        """
        Raw SQL method helper: commit + synchronous cache invalidation.

        Snapshots pending IDs for this type from session.info before commit
        (after_commit pops the entire pending dict), then uses the snapshot
        to perform synchronous invalidation after commit. This avoids accessing
        self attributes after commit (which would cause MissingGreenlet).

        Must call ``_register_pending_invalidation()`` before using this method.

        Usage::

            self._register_pending_invalidation(session, type(self), self.id)
            if commit:
                await self._commit_and_invalidate(session)
        """
        model_type = type(self)
        # Snapshot pending IDs (after_commit handler pops the entire pending dict)
        pending = session.info.get(_SESSION_PENDING_CACHE_KEY)
        captured_ids: set[Any] = set(pending.get(model_type, ())) if pending else set()

        await session.commit()

        await model_type._do_sync_invalidation(session, captured_ids)

    @classmethod
    async def _sync_invalidate_after_commit(
            cls,
            session: AsyncSession,
            instance_id: Any,
    ) -> None:
        """
        Synchronously invalidate this type's cache after another model's CRUD triggers commit.

        Use when: this type registered pending, but commit was triggered by another model's CRUD.
        E.g. adjust_foxcoins(): User pending registered, then Transaction.save(commit=True) triggers commit.

        If commit hasn't occurred (pending not consumed), this method safely no-ops.

        ``instance_id`` must be extracted before commit (self attributes expire after commit).

        Usage::

            user_id = self.id  # extract before commit
            self._register_pending_invalidation(session, type(self), user_id)
            transaction = await Transaction(...).save(session, commit=commit)
            await type(self)._sync_invalidate_after_commit(session, user_id)
        """
        await cls._do_sync_invalidation(session, {instance_id})

    @classmethod
    async def _do_sync_invalidation(
            cls,
            session: AsyncSession,
            captured_ids: set[Any],
    ) -> None:
        """
        Internal implementation of post-commit synchronous invalidation.

        Checks if pending was consumed by after_commit, and if so, performs
        synchronous invalidation and marks as synced.

        :param session: Database session
        :param captured_ids: Snapshot of pending IDs captured before commit (may contain sentinels)
        """
        current_pending = session.info.get(_SESSION_PENDING_CACHE_KEY)
        if current_pending and cls in current_pending:
            return  # Pending not consumed (commit didn't happen), skip

        sentinels = {_QUERY_ONLY_INVALIDATION, _FULL_MODEL_INVALIDATION}
        real_ids = captured_ids - sentinels
        needs_full = _FULL_MODEL_INVALIDATION in captured_ids

        try:
            if needs_full:
                await cls._invalidate_for_model()
            elif real_ids:
                for _id in real_ids:
                    await cls._invalidate_id_cache(_id)
                await cls._invalidate_query_caches()
            elif _QUERY_ONLY_INVALIDATION in captured_ids:
                await cls._invalidate_query_caches()
        except Exception as e:
            logger.error("Sync cache invalidation failed (%s): %s", cls.__name__, e)
        else:
            synced = session.info.get(_SESSION_SYNCED_CACHE_KEY)
            if isinstance(synced, dict):
                synced.setdefault(cls, set()).update(captured_ids)
