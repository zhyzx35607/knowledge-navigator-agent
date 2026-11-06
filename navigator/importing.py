"""Bounded UTF-8 file import. Uploaded filenames are labels, never disk paths."""
import base64
import binascii
from pathlib import PurePath

from pydantic import BaseModel, ConfigDict, Field

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 3 * 1024 * 1024


class ImportFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filename: str = Field(min_length=1, max_length=120)
    content_base64: str = Field(max_length=350000)


class ImportBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    files: list[ImportFile] = Field(min_length=1, max_length=20)
    collection_id: str | None = Field(default=None, max_length=64)
    entry_ids: list[str] = Field(default_factory=list, max_length=20)


def decode_files(files):
    result, total = [], 0
    for index, item in enumerate(files, 1):
        prefix = f"第 {index} 个文件"
        if any(char in item.filename for char in '/\\:') or any(ord(char) < 32 for char in item.filename):
            raise ValueError(f"{prefix}的文件名包含路径或控制字符；整批未导入")
        if PurePath(item.filename).suffix.lower() not in {".txt", ".md"}:
            raise ValueError(f"{prefix}仅支持 .txt / .md；整批未导入")
        try:
            data = base64.b64decode(item.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"{prefix}编码无效；整批未导入") from exc
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError(f"{prefix}超出大小限制（单文件 256 KiB，整批 2 MiB）；整批未导入")
        try:
            body = data.decode("utf-8-sig").strip()
        except UnicodeDecodeError as exc:
            raise ValueError(f"{prefix}不是有效 UTF-8 文本；整批未导入") from exc
        if not body or len(body) > 80000 or "\0" in body:
            raise ValueError(f"{prefix}须为非空文本且不超过 80000 字符，不得含 NUL；整批未导入")
        result.append({"filename": item.filename, "body": body})
    return result
