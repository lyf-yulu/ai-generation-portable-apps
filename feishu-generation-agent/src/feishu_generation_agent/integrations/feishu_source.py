import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    RequirementRequest,
    SourceType,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.integrations.feishu_sheet_export import (
    ExtractedSheetImage,
    SheetImageAnchor,
    parse_sheet_block_token,
)
from feishu_generation_agent.storage.files import FileStore, StoredFile


_BLOCK_TYPE_NAMES = {
    1: "page",
    2: "text",
    3: "heading1",
    4: "heading2",
    5: "heading3",
    6: "heading4",
    7: "heading5",
    8: "heading6",
    9: "heading7",
    10: "heading8",
    11: "heading9",
    12: "bullet",
    13: "ordered",
    14: "code",
    15: "quote",
    17: "todo",
    22: "divider",
    23: "file",
    27: "image",
    30: "sheet",
    31: "table",
    32: "table_cell",
}


def parse_feishu_url(url: str) -> tuple[SourceType, str]:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    valid_host = hostname in {"feishu.cn", "larksuite.com"} or hostname.endswith(
        (".feishu.cn", ".larksuite.com")
    )
    if parsed.scheme != "https" or not valid_host:
        raise ValueError("请输入 HTTPS 飞书或 LarkSuite 文档链接")

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("飞书文档链接缺少文档类型和 token")
    if parts[0] not in {SourceType.DOCX.value, SourceType.WIKI.value}:
        raise ValueError("只支持 docx 或 wiki 飞书文档")
    if len(parts) != 2 or not parts[1].strip():
        raise ValueError("飞书文档链接缺少 token")
    token = parts[1].strip()
    if token in {".", ".."} or "/" in token or "\\" in token:
        raise ValueError("飞书文档 token 无效")
    return SourceType(parts[0]), token


class FeishuDocumentSource:
    def __init__(
        self,
        client: Any,
        file_store: FileStore,
        *,
        sheet_exporter: Any | None = None,
    ) -> None:
        self._client = client
        self._file_store = file_store
        self._sheet_exporter = sheet_exporter

    async def get_revision(self, source_url: str) -> int:
        source_type, source_token = parse_feishu_url(source_url)
        document_id = await self._resolve_document_id(source_type, source_token)
        document = await self._get_document(document_id)
        return document["revision"]

    async def ingest(self, request: RequirementRequest) -> NormalizedDocument:
        source_type, source_token = parse_feishu_url(request.source_url)
        document_id = await self._resolve_document_id(source_type, source_token)
        document = await self._get_document(document_id)
        raw_blocks = await self._client.iter_items(
            f"/open-apis/docx/v1/documents/{document_id}/blocks"
        )
        blocks_by_id, source_ids = self._index_blocks(raw_blocks)
        ordered = self._ordered_blocks(blocks_by_id, source_ids)

        normalized_blocks: list[DocumentBlock] = []
        media_assets: list[MediaAsset] = []
        ingest_issues: list[str] = []
        text_lines: list[str] = []
        media_cache: dict[str, StoredFile | Exception] = {}
        normal_image_count = 0

        for order, (raw, path, row, column) in enumerate(ordered):
            block_id = raw["block_id"]
            block_type_number = raw.get("block_type")
            block_type = _BLOCK_TYPE_NAMES.get(
                block_type_number, f"block_{block_type_number}"
            )
            text = self._extract_text(raw, block_type)
            image_asset_id: str | None = None

            if block_type_number == 30:
                (
                    text,
                    sheet_text_lines,
                    sheet_assets,
                    sheet_issues,
                ) = await self._embedded_sheet(
                    raw,
                    document_id=document_id,
                )
                text_lines.extend(sheet_text_lines)
                media_assets.extend(sheet_assets)
                ingest_issues.extend(sheet_issues)
            else:
                if text:
                    text_lines.append(f"[block:{block_id}] {text}")

            if block_type_number == 27:
                normal_image_count += 1
                image_asset_id = f"image-{normal_image_count}"
                text_lines.append(f"[image:{image_asset_id}]")
                asset, issue = await self._media_asset(
                    raw,
                    document_id=document_id,
                    asset_id=image_asset_id,
                    cache=media_cache,
                )
                media_assets.append(asset)
                if issue is not None:
                    ingest_issues.append(issue)

            normalized_blocks.append(
                DocumentBlock(
                    block_id=block_id,
                    parent_id=self._string_or_none(raw.get("parent_id")),
                    block_type=block_type,
                    order=order,
                    path=path,
                    text=text,
                    table_row=row,
                    table_column=column,
                    image_asset_id=image_asset_id,
                )
            )

        return NormalizedDocument(
            document_id=document_id,
            title=document["title"],
            revision=document["revision"],
            source_type=source_type,
            source_token=source_token,
            blocks=normalized_blocks,
            text_view="\n".join(text_lines),
            media_assets=media_assets,
            ingest_issues=ingest_issues,
        )

    async def _embedded_sheet(
        self,
        raw: dict[str, Any],
        *,
        document_id: str,
    ) -> tuple[str, list[str], list[MediaAsset], list[str]]:
        block_id = raw["block_id"]
        sheet = raw.get("sheet")
        token = sheet.get("token") if isinstance(sheet, Mapping) else None
        if not isinstance(token, str) or not token:
            issue = (
                f"阻塞：内嵌电子表格读取失败（Block {block_id}）："
                "Sheet Block 缺少 token"
            )
            return "", [], [], [issue]
        try:
            ref = parse_sheet_block_token(token)
        except (TypeError, ValueError) as exc:
            issue = (
                f"阻塞：内嵌电子表格读取失败（Block {block_id}）：{exc}"
            )
            return "", [], [], [issue]

        if self._sheet_exporter is None:
            issue = (
                f"阻塞：内嵌电子表格 {ref.sheet_id} 读取失败"
                f"（Block {block_id}）：未配置电子表格导出器"
            )
            return "", [], [], [issue]

        try:
            extracted = await self._sheet_exporter.export(ref)
        except Exception as exc:
            issue = (
                f"阻塞：内嵌电子表格 {ref.sheet_id} 读取失败"
                f"（Block {block_id}）：{exc}"
            )
            return "", [], [], [issue]

        sheet_text_lines = list(extracted.text_lines)
        assets: list[MediaAsset] = []
        issues: list[str] = []
        for image in self._deduplicate_sheet_images(
            extracted.images
        ):
            asset, image_lines, issue = self._sheet_media_asset(
                image,
                document_id=document_id,
                sheet_id=ref.sheet_id,
                block_id=block_id,
            )
            assets.append(asset)
            sheet_text_lines.extend(image_lines)
            if issue is not None:
                issues.append(issue)
        return (
            "\n".join(extracted.text_lines),
            sheet_text_lines,
            assets,
            issues,
        )

    @staticmethod
    def _deduplicate_sheet_images(
        images: tuple[ExtractedSheetImage, ...],
    ) -> tuple[ExtractedSheetImage, ...]:
        by_hash: dict[str, ExtractedSheetImage] = {}
        for image in images:
            digest = hashlib.sha256(image.content).hexdigest()
            existing = by_hash.get(digest)
            if existing is None:
                by_hash[digest] = ExtractedSheetImage(
                    media_name=image.media_name,
                    content=image.content,
                    sha256=image.sha256,
                    anchors=image.anchors,
                )
                continue
            by_hash[digest] = ExtractedSheetImage(
                media_name=existing.media_name,
                content=existing.content,
                sha256=existing.sha256,
                anchors=(*existing.anchors, *image.anchors),
            )
        return tuple(by_hash.values())

    def _sheet_media_asset(
        self,
        image: ExtractedSheetImage,
        *,
        document_id: str,
        sheet_id: str,
        block_id: str,
    ) -> tuple[MediaAsset, list[str], str | None]:
        digest = hashlib.sha256(image.content).hexdigest()
        first_anchor = image.anchors[0] if image.anchors else None
        asset_id = self._sheet_asset_id(
            document_id=document_id,
            sheet_id=sheet_id,
            first_anchor=first_anchor,
            content_sha256=digest,
        )
        anchor_lines = [
            (
                f"[sheet-image:{asset_id} sheet:{anchor.source_sheet_id} "
                f"worksheet:{anchor.worksheet_name} "
                f"anchor:R{anchor.row + 1}C{anchor.column + 1}]"
            )
            for anchor in image.anchors
        ]
        if not image.anchors:
            anchor_lines.append(
                f"[sheet-image:{asset_id} sheet:{sheet_id} anchor:missing]"
            )
        try:
            if image.sha256 != digest:
                raise ValueError("电子表格图片内容哈希不匹配")
            if first_anchor is None:
                raise ValueError("电子表格图片缺少锚点")
            stored = self._file_store.save_input(
                document_id,
                image.media_name,
                image.content,
            )
        except Exception as exc:
            asset = MediaAsset(
                asset_id=asset_id,
                source_block_id=block_id,
                origin="feishu_embedded_sheet",
                local_path=Path("__missing__")
                / document_id
                / f"{digest}.missing",
                mime_type="application/octet-stream",
                size=0,
                sha256="",
                download_error=str(exc),
            )
            issue = (
                f"阻塞：内嵌电子表格素材 {asset_id} 保存失败"
                f"（Block {block_id}，Sheet {sheet_id}）：{exc}"
            )
            return asset, anchor_lines, issue

        asset = MediaAsset(
            asset_id=asset_id,
            source_block_id=block_id,
            origin="feishu_embedded_sheet",
            local_path=stored.local_path,
            mime_type=stored.mime_type,
            size=stored.size,
            sha256=stored.sha256,
            width=stored.width,
            height=stored.height,
        )
        return asset, anchor_lines, None

    @staticmethod
    def _sheet_asset_id(
        *,
        document_id: str,
        sheet_id: str,
        first_anchor: SheetImageAnchor | None,
        content_sha256: str,
    ) -> str:
        if first_anchor is None:
            anchor_part = "unanchored"
        else:
            worksheet_hash = hashlib.sha256(
                first_anchor.worksheet_name.encode("utf-8")
            ).hexdigest()[:12]
            anchor_part = (
                f"w{worksheet_hash}-r{first_anchor.row}-c{first_anchor.column}"
            )
        return (
            f"sheet-{document_id}-{sheet_id}-{anchor_part}-{content_sha256}"
        )

    async def _resolve_document_id(
        self,
        source_type: SourceType,
        source_token: str,
    ) -> str:
        if source_type == SourceType.DOCX:
            return source_token
        payload = await self._client.request_json(
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={"token": source_token},
        )
        node = self._nested_mapping(payload, "data", "node")
        if node is None:
            raise self._document_error(
                "飞书 wiki 节点响应无效",
                "wiki get_node response missing data.node",
            )
        if node.get("obj_type") != "docx":
            raise self._document_error(
                "该飞书 wiki 节点不是 docx 文档",
                f"wiki node obj_type={node.get('obj_type')!r}",
            )
        document_id = node.get("obj_token")
        if not isinstance(document_id, str) or not document_id:
            raise self._document_error(
                "飞书 wiki 节点缺少文档 token",
                "wiki node missing obj_token",
            )
        return document_id

    async def _get_document(self, document_id: str) -> dict[str, Any]:
        payload = await self._client.request_json(
            "GET", f"/open-apis/docx/v1/documents/{document_id}"
        )
        document = self._nested_mapping(payload, "data", "document")
        if document is None:
            raise self._document_error(
                "飞书 docx 文档信息响应无效",
                "document response missing data.document",
            )
        title = document.get("title")
        revision = document.get("revision_id")
        if not isinstance(title, str) or not isinstance(revision, int):
            raise self._document_error(
                "飞书 docx 文档标题或版本号无效",
                "document response has invalid title or revision_id",
            )
        return {"title": title, "revision": revision}

    @staticmethod
    def _index_blocks(
        raw_blocks: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        blocks_by_id: dict[str, dict[str, Any]] = {}
        source_ids: list[str] = []
        for raw in raw_blocks:
            block_id = raw.get("block_id")
            if not isinstance(block_id, str) or not block_id:
                raise FeishuDocumentSource._document_error(
                    "飞书文档包含无效 Block ID",
                    "block missing block_id",
                )
            if block_id in blocks_by_id:
                raise FeishuDocumentSource._document_error(
                    "飞书文档包含重复 Block ID",
                    f"duplicate block_id={block_id}",
                )
            blocks_by_id[block_id] = raw
            source_ids.append(block_id)
        return blocks_by_id, source_ids

    @staticmethod
    def _ordered_blocks(
        blocks_by_id: dict[str, dict[str, Any]],
        source_ids: list[str],
    ) -> list[tuple[dict[str, Any], list[str], int | None, int | None]]:
        ordered: list[
            tuple[dict[str, Any], list[str], int | None, int | None]
        ] = []
        children_by_id = FeishuDocumentSource._validate_block_references(
            blocks_by_id, source_ids
        )
        visited: set[str] = set()
        active: set[str] = set()

        def visit(
            block_id: str,
            parent_path: list[str],
            row: int | None = None,
            column: int | None = None,
        ) -> None:
            if block_id in visited or block_id not in blocks_by_id:
                return
            if block_id in active:
                raise FeishuDocumentSource._document_error(
                    "飞书文档 Block 层级存在循环",
                    f"block cycle at {block_id}",
                )
            active.add(block_id)
            raw = blocks_by_id[block_id]
            path = [*parent_path, block_id]
            ordered.append((raw, path, row, column))
            for child_id, child_row, child_column in children_by_id[block_id]:
                visit(child_id, path, child_row, child_column)
            active.remove(block_id)
            visited.add(block_id)

        roots = [
            block_id
            for block_id in source_ids
            if blocks_by_id[block_id].get("parent_id") not in blocks_by_id
        ]
        for block_id in roots:
            visit(block_id, [])
        for block_id in source_ids:
            visit(block_id, [])
        return ordered

    @staticmethod
    def _validate_block_references(
        blocks_by_id: dict[str, dict[str, Any]],
        source_ids: list[str],
    ) -> dict[str, list[tuple[str, int | None, int | None]]]:
        children_by_id: dict[
            str, list[tuple[str, int | None, int | None]]
        ] = {}
        referenced_parent: dict[str, str] = {}

        for parent_id in source_ids:
            children = FeishuDocumentSource._children(blocks_by_id[parent_id])
            children_by_id[parent_id] = children
            for child_id, _row, _column in children:
                if child_id not in blocks_by_id:
                    raise FeishuDocumentSource._document_error(
                        "飞书文档引用了不存在的 Block",
                        f"parent {parent_id} references missing child {child_id}",
                    )
                if child_id in referenced_parent:
                    raise FeishuDocumentSource._document_error(
                        "飞书文档 Block 被多个父节点引用",
                        f"child {child_id} referenced by "
                        f"{referenced_parent[child_id]} and {parent_id}",
                    )
                referenced_parent[child_id] = parent_id

        for block_id in source_ids:
            raw_parent = blocks_by_id[block_id].get("parent_id")
            if raw_parent == "":
                raw_parent = None
            if raw_parent is not None and (
                not isinstance(raw_parent, str) or not raw_parent
            ):
                raise FeishuDocumentSource._document_error(
                    "飞书文档包含无效父 Block ID",
                    f"block {block_id} has invalid parent_id",
                )
            declared_parent = raw_parent if isinstance(raw_parent, str) else None
            if declared_parent is not None and declared_parent not in blocks_by_id:
                raise FeishuDocumentSource._document_error(
                    "飞书文档引用了不存在的父 Block",
                    f"block {block_id} declares missing parent {declared_parent}",
                )
            actual_parent = referenced_parent.get(block_id)
            if declared_parent != actual_parent:
                raise FeishuDocumentSource._document_error(
                    "飞书文档 Block 父节点声明与引用不一致",
                    f"block {block_id}: declared parent={declared_parent!r}, "
                    f"referenced parent={actual_parent!r}",
                )

        return children_by_id

    @staticmethod
    def _children(raw: dict[str, Any]) -> list[tuple[str, int | None, int | None]]:
        children = raw.get("children", [])
        if children is None:
            children = []
        if not isinstance(children, list) or not all(
            isinstance(item, str) and item for item in children
        ):
            raise FeishuDocumentSource._document_error(
                "飞书 Block 子节点列表无效",
                f"block {raw.get('block_id')}: children is not a list of IDs",
            )
        child_ids = list(children)
        if len(child_ids) != len(set(child_ids)):
            raise FeishuDocumentSource._document_error(
                "飞书 Block 包含重复子节点",
                f"block {raw.get('block_id')}: duplicate child ID",
            )
        if raw.get("block_type") != 31:
            return [(child_id, None, None) for child_id in child_ids]

        table = raw.get("table")
        if not isinstance(table, Mapping):
            raise FeishuDocumentSource._document_error(
                "飞书表格内容无效",
                f"table block {raw.get('block_id')}: missing table object",
            )
        cells = table.get("cells", [])
        property_value = table.get("property", {})
        if not isinstance(cells, list) or not isinstance(property_value, Mapping):
            raise FeishuDocumentSource._document_error(
                "飞书表格行列信息无效",
                f"table block {raw.get('block_id')}: invalid cells or dimensions",
            )
        columns = property_value.get("column_size")
        rows = property_value.get("row_size")
        if (
            not isinstance(columns, int)
            or isinstance(columns, bool)
            or columns <= 0
            or not isinstance(rows, int)
            or isinstance(rows, bool)
            or rows <= 0
            or len(cells) != rows * columns
            or not all(isinstance(cell, str) and cell for cell in cells)
        ):
            raise FeishuDocumentSource._document_error(
                "飞书表格行列信息无效",
                f"table block {raw.get('block_id')}: invalid cells or dimensions",
            )
        if len(cells) != len(set(cells)):
            raise FeishuDocumentSource._document_error(
                "飞书表格包含重复单元格",
                f"table block {raw.get('block_id')}: duplicate cell ID",
            )
        if child_ids and child_ids != cells:
            raise FeishuDocumentSource._document_error(
                "飞书表格 children 与 cells 不一致",
                f"table block {raw.get('block_id')}: children do not match cells",
            )
        return [
            (cell_id, index // columns, index % columns)
            for index, cell_id in enumerate(cells)
        ]

    @staticmethod
    def _extract_text(raw: dict[str, Any], block_type: str) -> str:
        payload = raw.get(block_type)
        if not isinstance(payload, Mapping):
            return ""
        fragments: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                text_run = value.get("text_run")
                if isinstance(text_run, Mapping):
                    content = text_run.get("content")
                    if isinstance(content, str):
                        fragments.append(content)
                    return
                equation = value.get("equation")
                if isinstance(equation, Mapping):
                    content = equation.get("content")
                    if isinstance(content, str):
                        fragments.append(content)
                    return
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(payload.get("elements", []))
        return "".join(fragments).strip()

    async def _media_asset(
        self,
        raw: dict[str, Any],
        *,
        document_id: str,
        asset_id: str,
        cache: dict[str, StoredFile | Exception],
    ) -> tuple[MediaAsset, str | None]:
        image = raw.get("image")
        file_token = image.get("token") if isinstance(image, Mapping) else None
        block_id = raw["block_id"]
        if not isinstance(file_token, str) or not file_token:
            error = "图片 Block 缺少 file_token"
            return self._failed_media_asset(
                document_id, asset_id, block_id, None, error
            )

        cached = cache.get(file_token)
        if cached is None:
            try:
                content, _content_type = await self._client.download_media(file_token)
                cached = self._file_store.save_input(
                    document_id, f"{asset_id}.image", content
                )
            except Exception as exc:
                cached = exc
            cache[file_token] = cached

        if isinstance(cached, Exception):
            return self._failed_media_asset(
                document_id, asset_id, block_id, file_token, str(cached)
            )

        width = image.get("width") if isinstance(image, Mapping) else None
        height = image.get("height") if isinstance(image, Mapping) else None
        return (
            MediaAsset(
                asset_id=asset_id,
                source_block_id=block_id,
                origin="feishu",
                file_token=file_token,
                local_path=cached.local_path,
                mime_type=cached.mime_type,
                size=cached.size,
                sha256=cached.sha256,
                width=cached.width if cached.width is not None else width,
                height=cached.height if cached.height is not None else height,
            ),
            None,
        )

    @staticmethod
    def _failed_media_asset(
        document_id: str,
        asset_id: str,
        block_id: str,
        file_token: str | None,
        error: str,
    ) -> tuple[MediaAsset, str]:
        return (
            MediaAsset(
                asset_id=asset_id,
                source_block_id=block_id,
                origin="feishu",
                file_token=file_token,
                local_path=Path("__missing__")
                / document_id
                / f"{asset_id}.missing",
                mime_type="application/octet-stream",
                size=0,
                sha256="",
                download_error=error,
            ),
            f"阻塞：素材 {asset_id} 下载失败（Block {block_id}）：{error}",
        )

    @staticmethod
    def _nested_mapping(
        payload: Mapping[str, Any], *keys: str
    ) -> Mapping[str, Any] | None:
        current: Any = payload
        for key in keys:
            if not isinstance(current, Mapping):
                return None
            current = current.get(key)
        return current if isinstance(current, Mapping) else None

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _document_error(message: str, technical_detail: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.DOCUMENT,
                message=message,
                technical_detail=technical_detail,
                retryable=False,
            )
        )
