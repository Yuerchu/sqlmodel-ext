# 给列表端点加分页

**目标**：让一个列表端点接受 `?offset=`、`?limit=`、`?desc=`、`?order=`、`?created_after_datetime=` 等查询参数，并返回 `{count, items}` 形式的响应。

**前置条件**：

- 你已经有一个 FastAPI 端点
- 你的模型继承了 `TableBaseMixin` 或 `UUIDTableBaseMixin`
- 你有一个 `XxxResponse` DTO

## 1. 把请求参数声明为 FastAPI 依赖

```python
from typing import Annotated
from fastapi import Depends
from sqlmodel_ext import TableViewRequest

TableViewDep = Annotated[TableViewRequest, Depends()]
```

`TableViewRequest` 同时包含分页（`offset` / `limit` / `desc` / `order`）和时间过滤（`created_after_datetime` / `created_before_datetime` / `updated_after_datetime` / `updated_before_datetime`）。FastAPI 的 `Depends()` 会自动把查询字符串解析成这个对象。

## 2. 在端点中调用 `get_with_count()`

```python
from sqlmodel_ext import ListResponse

@router.get("", response_model=ListResponse[ArticleResponse])
async def list_articles(
    session: SessionDep,
    table_view: TableViewDep,
) -> ListResponse[Article]:
    return await Article.get_with_count(
        session,
        Article.is_published == True,
        table_view=table_view,
    )
```

`get_with_count()` 内部并行执行 `COUNT(*)` 和 `SELECT ... LIMIT N OFFSET M`，组装成 `ListResponse[T]` 返回。

## 3. 客户端怎么调用

```http
GET /articles?offset=0&limit=20&desc=true&order=created_at&created_after_datetime=2026-01-01T00:00:00
```

返回：

```json
{
  "count": 142,
  "items": [
    { "id": "...", "title": "...", "created_at": "...", "..." : "..." }
  ]
}
```

## 默认值

| 参数 | 默认值 | 上限 |
|------|--------|------|
| `offset` | `0` | — |
| `limit` | `50` | `100` |
| `desc` | `True` | — |
| `order` | `"created_at"` | 仅支持 `"created_at"` 和 `"updated_at"` |

如果你需要按其他字段排序，跳过 `table_view`，自己传 `order_by=` 给 `get()` / `get_with_count()`。

## 常见陷阱

- **`response_model` 必须用 `ListResponse[ArticleResponse]`**，不能写成 `list[ArticleResponse]`——后者会让 FastAPI 把 `count` 字段也当成 list item 来序列化。
- **时间区间是左闭右开** `[after, before)`。`created_after_datetime=2026-01-01` + `created_before_datetime=2026-02-01` 表示"整个 1 月"。
- **`order` 只能是 `created_at` 或 `updated_at`**（`Literal` 限制）。如果你的客户端传了别的字符串，FastAPI 会返回 `422 Unprocessable Entity`。

## 相关参考

- [`TableViewRequest` / `ListResponse` 完整字段](/reference/pagination-types)
- [`get_with_count()` 完整签名](/reference/crud-methods#get-with-count)
