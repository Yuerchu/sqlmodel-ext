# Paginate a list endpoint

**Goal**: make a list endpoint accept query parameters like `?offset=`, `?limit=`, `?desc=`, `?order=`, `?created_after_datetime=`, and return a `{count, items}` response.

**Prerequisites**:

- You already have a FastAPI endpoint
- Your model inherits `TableBaseMixin` or `UUIDTableBaseMixin`
- You already have an `XxxResponse` DTO

## 1. Declare the request DTO as a FastAPI dependency

```python
from typing import Annotated
from fastapi import Depends
from sqlmodel_ext import TableViewRequest

TableViewDep = Annotated[TableViewRequest, Depends()]
```

`TableViewRequest` carries both pagination (`offset` / `limit` / `desc` / `order`) and time filtering (`created_after_datetime` / `created_before_datetime` / `updated_after_datetime` / `updated_before_datetime`). FastAPI's `Depends()` parses query strings into this DTO automatically.

## 2. Call `get_with_count()` in the endpoint

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

`get_with_count()` runs `COUNT(*)` and `SELECT ... LIMIT N OFFSET M` in sequence and assembles them into a `ListResponse[T]`.

## 3. How clients call it

```http
GET /articles?offset=0&limit=20&desc=true&order=created_at&created_after_datetime=2026-01-01T00:00:00
```

Returns:

```json
{
  "count": 142,
  "items": [
    { "id": "...", "title": "...", "created_at": "...", "..." : "..." }
  ]
}
```

## Defaults

| Parameter | Default | Cap |
|-----------|---------|-----|
| `offset` | `0` | — |
| `limit` | `50` | `100` |
| `desc` | `True` | — |
| `order` | `"created_at"` | only `"created_at"` and `"updated_at"` are accepted |

If you need to sort by a different field, skip `table_view` and pass `order_by=` to `get()` / `get_with_count()` directly.

## Common pitfalls

- **`response_model` must be `ListResponse[ArticleResponse]`**, not `list[ArticleResponse]` — the latter would make FastAPI try to serialize the `count` field as a list item.
- **Time intervals are half-open** `[after, before)`. `created_after_datetime=2026-01-01` + `created_before_datetime=2026-02-01` means "all of January".
- **`order` only accepts `created_at` or `updated_at`** (`Literal` constraint). Any other string yields a `422 Unprocessable Entity`.

## Related reference

- [`TableViewRequest` / `ListResponse` field details](/en/reference/pagination-types)
- [`get_with_count()` full signature](/en/reference/crud-methods#get-with-count)
