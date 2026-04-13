# 基础类

::: tip
本页是参考文档。要看怎么用这些类构建模型，去 [教程](/tutorials/01-getting-started) 或 [操作指南](/how-to/)。
:::

## `SQLModelBase`

```python
from sqlmodel_ext import SQLModelBase
```

所有 sqlmodel-ext 模型的根类。继承自 `SQLModel`，使用自定义元类 `__DeclarativeMeta`。

**`model_config`**：

| 键 | 值 | 说明 |
|---|---|---|
| `use_attribute_docstrings` | `True` | 字段下方的 `"""..."""` 自动作为字段描述 |
| `validate_by_name` | `True` | 允许通过字段名验证（即使有 alias） |
| `extra` | `'forbid'` | 传入未定义的字段会抛 `ValidationError` |

**类方法**：

```python
@classmethod
def get_computed_field_names(cls) -> set[str]
```

返回所有 `@computed_field` 字段的名称集合。

**继承场景**：

- `class XxxBase(SQLModelBase)` — 纯数据模型（不建表），用于 API 输入/输出
- `class Xxx(XxxBase, TableBaseMixin, table=True)` — 建表模型

## `ExtraIgnoreModelBase`

```python
from sqlmodel_ext import ExtraIgnoreModelBase
```

继承自 `SQLModelBase`，但 `extra='ignore'`：未知字段被静默忽略，同时记录 WARNING 日志。

**`model_config`**：

| 键 | 值 | 说明 |
|---|---|---|
| `use_attribute_docstrings` | `True` | 同 `SQLModelBase` |
| `validate_by_name` | `True` | 同 `SQLModelBase` |
| `extra` | `'ignore'` | 未知字段被忽略（不报错） |

**校验器**：

```python
@model_validator(mode='before')
@classmethod
def _warn_unknown_fields(cls, data: Any) -> Any
```

如果输入是 dict 且包含未声明的字段，记录 WARNING 日志。`alias` 和 `validation_alias` 也算已知字段。

**适用场景**：第三方 API 响应、外部 WebSocket 消息、JSON schema 可能变化的输入。

## `TableBaseMixin`

```python
from sqlmodel_ext import TableBaseMixin
```

为模型添加自增整数主键和 CRUD 方法。

**继承自**：`AsyncAttrs`（提供 `await obj.awaitable_attrs.xxx` 语法）。

**类标记**：

```python
_has_table_mixin: ClassVar[bool] = True
```

让元类识别"这是 table 类"，自动添加 `table=True`。

**字段**：

| 字段 | 类型 | 数据库行为 |
|------|------|----------|
| `id` | `int \| None` | 主键，自动生成（`SERIAL` / `INTEGER PRIMARY KEY`） |
| `created_at` | `datetime` | 创建时自动设置，`default_factory=now` |
| `updated_at` | `datetime` | 每次 UPDATE 自动刷新，`onupdate=now` |

**方法**：CRUD 方法签名见 [CRUD 方法](./crud-methods)。

## `UUIDTableBaseMixin`

```python
from sqlmodel_ext import UUIDTableBaseMixin
```

`TableBaseMixin` 的 UUID 主键变体。

**字段**：

| 字段 | 类型 | 数据库行为 |
|------|------|----------|
| `id` | `uuid.UUID` | 主键，`default_factory=uuid.uuid4` |
| `created_at` | `datetime` | 同 `TableBaseMixin` |
| `updated_at` | `datetime` | 同 `TableBaseMixin` |

**类型精确的 override**：

`UUIDTableBaseMixin` 重载了 `get_one()` / `get_exist_one()`，参数 `id` 类型为 `uuid.UUID`（而非 `int`）。

## `RecordNotFoundError`

```python
from sqlmodel_ext import RecordNotFoundError
```

未安装 FastAPI 时，`get_exist_one()` 找不到记录抛出此异常。安装 FastAPI 时改为抛 `HTTPException(404)`。

**判定逻辑**：模块导入时检测 `import fastapi`，结果缓存在 `_HAS_FASTAPI`。
