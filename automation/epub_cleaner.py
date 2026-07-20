#!/usr/bin/env python3
"""Turn a raw PressReader EPUB+ export into an article-first EPUB.

The duplicate selection rule is adapted from Commodore64user's MIT-licensed
pressreader_epub_deduplicator.  This standalone implementation does not need
Calibre: it also removes print-page chrome and rebuilds article navigation for
KOReader.
"""

from __future__ import annotations

import hashlib
import html
import os
import posixpath
import re
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
XLINK_NS = "http://www.w3.org/1999/xlink"

ET.register_namespace("", XHTML_NS)

@dataclass(frozen=True)
class CleanupStats:
    articles_found: int
    articles_kept: int
    duplicates_removed: int
    page_documents_removed: int
    assets_removed: int
    original_bytes: int
    cleaned_bytes: int


@dataclass
class Article:
    document: str
    element: ET.Element
    article_id: str
    title: str
    page_number: int
    fingerprint: str
    unique_key: str

    @property
    def href(self) -> str:
        return f"{self.document}#{self.article_id}"


@dataclass
class NavigationNode:
    title: str
    article: Article | None = None
    children: list["NavigationNode"] = field(default_factory=list)

    def first_article(self) -> Article | None:
        if self.article:
            return self.article
        for child in self.children:
            article = child.first_article()
            if article:
                return article
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _classes(element: ET.Element) -> set[str]:
    return set(element.get("class", "").split())


def _has_class(element: ET.Element, name: str) -> bool:
    return name in _classes(element)


def _text(element: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _page_number(path: str) -> int:
    match = re.search(r"(?:^|/)page-(\d+)(?:/|$)", path)
    return int(match.group(1)) if match else 0


def _find_parent(root: ET.Element, target: ET.Element) -> ET.Element | None:
    for parent in root.iter():
        if target in list(parent):
            return parent
    return None


def _remove_matching(root: ET.Element, classes: set[str]) -> None:
    for element in list(root.iter()):
        if _classes(element) & classes:
            parent = _find_parent(root, element)
            if parent is not None:
                parent.remove(element)


def _semantic_article_markup(root: ET.Element) -> None:
    replacements = {
        "title": "h1",
        "subtitle": "h2",
        "byline": "p",
        "img-byline": "p",
        "img-text": "p",
        "annotation": "blockquote",
    }
    for element in root.iter():
        classes = _classes(element)
        for class_name, tag_name in replacements.items():
            if class_name in classes:
                element.tag = f"{{{XHTML_NS}}}{tag_name}"
                break
        element.attrib.pop("class", None)
        element.attrib.pop("style", None)
        element.attrib.pop("width", None)
        element.attrib.pop("height", None)
        if element.text:
            element.text = element.text.replace("\u00ad", "")
        if element.tail:
            element.tail = element.tail.replace("\u00ad", "")


def _article_fingerprint(element: ET.Element) -> str:
    paragraphs = [
        re.sub(r"\s+", " ", _text(item)).strip().casefold()
        for item in element.iter()
        if _local_name(item.tag) == "p"
    ]
    body = "".join(paragraphs)
    # Very short blurbs and image-only cartoons are unsafe to deduplicate by
    # text alone. Long PressReader duplicates contain the complete same body.
    return hashlib.sha256(body.encode("utf-8")).hexdigest() if len(body) >= 80 else ""


def _article_title(element: ET.Element) -> str:
    for item in element.iter():
        if _has_class(item, "title"):
            value = _text(item)
            if value:
                return value
    return "Untitled article"


def _choose_duplicate(articles: list[Article]) -> Article:
    """Keep the first page in the final near-consecutive occurrence block."""
    ordered = sorted(articles, key=lambda item: item.page_number)
    candidate = ordered[-1]
    for item in reversed(ordered[:-1]):
        if candidate.page_number - item.page_number <= 2:
            candidate = item
        else:
            break
    return candidate


def _parse_xml(data: bytes, name: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as err:
        raise ValueError(f"Invalid XML in {name}: {err}") from err


def _serialize(root: ET.Element, namespace: str) -> bytes:
    ET.register_namespace("", namespace)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _opf_path(files: dict[str, bytes]) -> str:
    root = _parse_xml(files["META-INF/container.xml"], "META-INF/container.xml")
    node = root.find(f".//{{{CONTAINER_NS}}}rootfile")
    if node is None or not node.get("full-path"):
        raise ValueError("EPUB container does not identify its package document")
    return posixpath.normpath(node.get("full-path", ""))


def _resolve(base_document: str, reference: str) -> str:
    clean = reference.split("#", 1)[0].split("?", 1)[0]
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_document), clean))


def _used_images(document: str, element: ET.Element, available: set[str]) -> set[str]:
    result: set[str] = set()
    for item in element.iter():
        for attr in ("src", "href", f"{{{XLINK_NS}}}href"):
            value = item.get(attr)
            if value:
                target = _resolve(document, value)
                if target in available and target.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
                    result.add(target)
    return result


def _navigation_href(navigation_document: str, article: Article) -> str:
    relative = posixpath.relpath(article.document, posixpath.dirname(navigation_document))
    return f"{relative}#{article.article_id}"


def _navigation_html(nodes: list[NavigationNode], toc_path: str) -> str:
    items = []
    for node in nodes:
        if node.article:
            label = f'<a href="{html.escape(_navigation_href(toc_path, node.article), quote=True)}">{html.escape(node.title)}</a>'
        else:
            label = f"<span>{html.escape(node.title)}</span>"
        children = f"<ol>{_navigation_html(node.children, toc_path)}</ol>" if node.children else ""
        items.append(f"<li>{label}{children}</li>")
    return "\n".join(items)


def _build_toc_xhtml(nodes: list[NavigationNode], toc_path: str) -> bytes:
    document = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="{XHTML_NS}"><head><title>Contents</title></head>
<body><h1>Contents</h1><ol>{_navigation_html(nodes, toc_path)}</ol></body></html>"""
    return document.encode("utf-8")


def _nav_label(point: ET.Element) -> str:
    label = point.find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text")
    return _text(label) if label is not None else ""


def _content_source(point: ET.Element) -> str:
    content = point.find(f"{{{NCX_NS}}}content")
    return content.get("src", "") if content is not None else ""


def _navigation_key(ncx_path: str, source: str) -> str:
    anchor = source.split("#", 1)[1] if "#" in source else ""
    return f"{_resolve(ncx_path, source)}#{anchor}"


def _extract_navigation(old_data: bytes, articles: list[Article], ncx_path: str) -> list[NavigationNode]:
    root = _parse_xml(old_data, "toc.ncx")
    article_lookup = {article.unique_key: article for article in articles}
    included: set[str] = set()

    def transform(point: ET.Element) -> list[NavigationNode]:
        source = _content_source(point)
        article = article_lookup.get(_navigation_key(ncx_path, source)) if source else None
        if article:
            included.add(article.unique_key)
            source_label = _nav_label(point)
            title = source_label if article.title == "Untitled article" and source_label else article.title
            return [NavigationNode(title, article=article)]
        children: list[NavigationNode] = []
        for child in point.findall(f"{{{NCX_NS}}}navPoint"):
            children.extend(transform(child))
        if not children:
            return []
        label = _nav_label(point)
        point_id = point.get("id", "")
        is_page = bool(re.match(r"^page(?:\s|$)", label, re.I)) or point_id.startswith("page-")
        is_section = bool(label) and not is_page and (
            point_id.startswith("s-") or source.endswith("#top")
        )
        return [NavigationNode(label, children=children)] if is_section else children

    nodes: list[NavigationNode] = []
    nav_map = root.find(f"{{{NCX_NS}}}navMap")
    if nav_map is not None:
        for point in nav_map.findall(f"{{{NCX_NS}}}navPoint"):
            nodes.extend(transform(point))
    for article in articles:
        if article.unique_key not in included:
            nodes.append(NavigationNode(article.title, article=article))
    return _deduplicate_navigation(nodes)


def _deduplicate_navigation(nodes: list[NavigationNode]) -> list[NavigationNode]:
    """Hide adjacent continuation records without discarding their content.

    Some magazine exports represent a long article as multiple ``art-cnt``
    elements, normally separated by an image-only print page.  Their bodies
    differ, so all records must stay in the spine, but repeating the headline
    for every record makes KOReader's table of contents misleading.
    """
    last_article_by_title: dict[str, Article] = {}

    def prune(items: list[NavigationNode]) -> list[NavigationNode]:
        result: list[NavigationNode] = []
        for node in items:
            if node.article:
                key = re.sub(r"\s+", " ", node.title).strip().casefold()
                previous = last_article_by_title.get(key)
                last_article_by_title[key] = node.article
                if previous and 0 < node.article.page_number - previous.page_number <= 2:
                    continue
                result.append(node)
                continue
            node.children = prune(node.children)
            if node.children:
                result.append(node)
        return result

    return prune(nodes)


def _build_ncx(old_data: bytes, nodes: list[NavigationNode], ncx_path: str) -> bytes:
    old = _parse_xml(old_data, "toc.ncx")
    root = ET.Element(f"{{{NCX_NS}}}ncx", {"version": "2005-1"})
    for name in ("head", "docTitle", "docAuthor"):
        source = old.find(f"{{{NCX_NS}}}{name}")
        if source is not None:
            root.append(source)
    nav_map = ET.SubElement(root, f"{{{NCX_NS}}}navMap")
    order = 0

    def append_nodes(parent: ET.Element, items: list[NavigationNode]) -> None:
        nonlocal order
        for node in items:
            article = node.first_article()
            if article is None:
                continue
            order += 1
            point = ET.SubElement(parent, f"{{{NCX_NS}}}navPoint", {
                "id": f"clean-nav-{order}", "playOrder": str(order),
            })
            label = ET.SubElement(point, f"{{{NCX_NS}}}navLabel")
            ET.SubElement(label, f"{{{NCX_NS}}}text").text = node.title
            ET.SubElement(point, f"{{{NCX_NS}}}content", {
                "src": _navigation_href(ncx_path, article),
            })
            append_nodes(point, node.children)

    append_nodes(nav_map, nodes)
    return _serialize(root, NCX_NS)


def _clean_document(document: str, root: ET.Element, keep: set[str]) -> tuple[bytes | None, list[Article]]:
    body = next((item for item in root.iter() if _local_name(item.tag) == "body"), None)
    head = next((item for item in root.iter() if _local_name(item.tag) == "head"), None)
    if body is None or head is None:
        return None, []

    retained: list[Article] = []
    for element in list(root.iter()):
        if not _has_class(element, "art-cnt"):
            continue
        article_id = element.get("id", "")
        key = f"{document}#{article_id}"
        if key not in keep:
            continue
        title = _article_title(element)
        _remove_matching(element, {"legal-header", "art-header", "art-divider", "art-thumb-img"})
        _semantic_article_markup(element)
        retained.append(Article(
            document=document, element=element, article_id=article_id,
            title=title, page_number=_page_number(document),
            fingerprint="", unique_key=key,
        ))

    if not retained:
        return None, []
    for child in list(body):
        body.remove(child)
    for article in retained:
        body.append(article.element)
    for child in list(head):
        if _local_name(child.tag) == "link":
            head.remove(child)
    return _serialize(root, XHTML_NS), retained


def _update_opf(
    data: bytes,
    opf_path: str,
    kept_documents: list[str],
    kept_assets: set[str],
    toc_path: str,
    ncx_path: str,
) -> bytes:
    root = _parse_xml(data, opf_path)
    manifest = root.find(f"{{{OPF_NS}}}manifest")
    spine = root.find(f"{{{OPF_NS}}}spine")
    if manifest is None or spine is None:
        raise ValueError("EPUB package is missing its manifest or spine")

    opf_dir = posixpath.dirname(opf_path)
    wanted = set(kept_documents) | kept_assets | {
        toc_path, ncx_path, posixpath.join(opf_dir, "cover.xhtml"),
    }
    ids_by_path: dict[str, str] = {}
    for item in list(manifest):
        target = _resolve(opf_path, item.get("href", ""))
        if target not in wanted:
            manifest.remove(item)
        elif item.get("id"):
            ids_by_path[target] = item.get("id", "")
    for item in list(spine):
        spine.remove(item)
    spine.set("toc", ids_by_path[ncx_path])
    ordered_documents = [path for path in (posixpath.join(opf_dir, "cover.xhtml"), toc_path, *kept_documents) if path in ids_by_path]
    for document in ordered_documents:
        ET.SubElement(spine, f"{{{OPF_NS}}}itemref", {"idref": ids_by_path[document]})
    return _serialize(root, OPF_NS)


def clean_pressreader_epub(path: Path) -> CleanupStats:
    """Clean *path* atomically, returning a summary of the transformation."""
    path = Path(path)
    original_bytes = path.stat().st_size
    with zipfile.ZipFile(path) as archive:
        files = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    if files.get("mimetype", b"").strip() != b"application/epub+zip":
        raise ValueError("Not a standard EPUB")

    opf_path = _opf_path(files)
    opf_root = _parse_xml(files[opf_path], opf_path)
    publishers = " ".join(
        _text(item) for item in opf_root.iter()
        if _local_name(item.tag) in {"creator", "publisher"}
    )
    if "NewspaperDirect" not in publishers:
        raise ValueError("Not a PressReader/NewspaperDirect EPUB")
    opf_dir = posixpath.dirname(opf_path)
    page_documents = sorted(
        name for name in files
        if re.search(r"(?:^|/)page-\d+/page-\d+\.xhtml$", name, re.I)
    )

    parsed: dict[str, ET.Element] = {}
    articles: list[Article] = []
    for document in page_documents:
        root = _parse_xml(files[document], document)
        parsed[document] = root
        index = 0
        for element in root.iter():
            if not _has_class(element, "art-cnt"):
                continue
            index += 1
            article_id = element.get("id") or f"article-{index}"
            element.set("id", article_id)
            fingerprint = _article_fingerprint(element)
            unique_key = f"{document}#{article_id}"
            articles.append(Article(
                document=document, element=element, article_id=article_id,
                title=_article_title(element), page_number=_page_number(document),
                fingerprint=fingerprint, unique_key=unique_key,
            ))
    if not articles:
        raise ValueError("No PressReader articles were found")

    duplicate_groups: dict[str, list[Article]] = defaultdict(list)
    keep_keys: set[str] = set()
    for article in articles:
        if article.fingerprint:
            duplicate_groups[article.fingerprint].append(article)
        else:
            keep_keys.add(article.unique_key)
    for group in duplicate_groups.values():
        keep_keys.add(_choose_duplicate(group).unique_key)

    cleaned_documents: dict[str, bytes] = {}
    kept_articles: list[Article] = []
    for document in page_documents:
        cleaned, retained = _clean_document(document, parsed[document], keep_keys)
        if cleaned:
            cleaned_documents[document] = cleaned
            kept_articles.extend(retained)
    toc_path = posixpath.join(opf_dir, "toc.xhtml")
    ncx_path = posixpath.join(opf_dir, "toc.ncx")
    cover_document = posixpath.join(opf_dir, "cover.xhtml")
    available = set(files)
    kept_assets: set[str] = set()
    if cover_document in files:
        cover_root = _parse_xml(files[cover_document], cover_document)
        kept_assets |= _used_images(cover_document, cover_root, available)
    for article in kept_articles:
        kept_assets |= _used_images(article.document, article.element, available)

    navigation = _extract_navigation(files[ncx_path], kept_articles, ncx_path)
    files[toc_path] = _build_toc_xhtml(navigation, toc_path)
    files[ncx_path] = _build_ncx(files[ncx_path], navigation, ncx_path)
    files[opf_path] = _update_opf(
        files[opf_path], opf_path, list(cleaned_documents), kept_assets,
        toc_path, ncx_path,
    )
    files.update(cleaned_documents)

    always_keep = {"mimetype", "META-INF/container.xml", opf_path, toc_path, ncx_path}
    if cover_document in files:
        always_keep.add(cover_document)
    output_names = always_keep | set(cleaned_documents) | kept_assets
    removed_assets = sum(
        1 for name in available - output_names
        if name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"))
    )

    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", files["mimetype"], compress_type=zipfile.ZIP_STORED)
            for name in sorted(output_names - {"mimetype"}):
                archive.writestr(name, files[name])
        with zipfile.ZipFile(temporary) as check:
            if check.read("mimetype").strip() != b"application/epub+zip":
                raise ValueError("Cleaned file failed EPUB validation")
            _parse_xml(check.read(opf_path), opf_path)
            _parse_xml(check.read(ncx_path), ncx_path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

    return CleanupStats(
        articles_found=len(articles), articles_kept=len(kept_articles),
        duplicates_removed=len(articles) - len(kept_articles),
        page_documents_removed=len(page_documents) - len(cleaned_documents),
        assets_removed=removed_assets, original_bytes=original_bytes,
        cleaned_bytes=path.stat().st_size,
    )


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", nargs="+", type=Path)
    args = parser.parse_args()
    for epub in args.epub:
        print(epub, clean_pressreader_epub(epub))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
