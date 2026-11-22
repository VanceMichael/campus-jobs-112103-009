"""同一招聘需求判定（跨渠道转载归并）。

判定键 = 企业 + 规范化标题 + 地点 + 用工性质：
- 同一岗位被不同渠道转载时，四者一致，归并为一个招聘需求；
- 同一企业的不同地点或不同用工性质（实习/正式/短期项目）仍是不同岗位；
- 标题相似不算同一岗位：技能要求以各岗位内容为准，不参与归并。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title).lower()
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def identity_key(
    employer_ref: str, title: str, location: str, employment_kind: str
) -> tuple[str, str, str, str]:
    return (employer_ref, normalize_title(title), location, employment_kind)


def canonical_id_for(key: tuple[str, str, str, str]) -> str:
    digest = hashlib.sha1("|".join(key).encode("utf-8")).hexdigest()[:10]
    return f"C-{digest}"
