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
import unicodedata
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
READER_STYLE_NAME = "pressko.css"
PULLQUOTE_EXCLUDED_CLASSES = {
    "annotation", "article-header", "byline", "caption", "image-credit",
    "img-art", "img-byline", "img-text", "legend", "media", "standfirst",
}
PULLQUOTE_SENTENCE_END = re.compile(r"[.!?…][\s\"'’”»)]*$")
PULLQUOTE_OPENING_QUOTE = re.compile(r"^[\s\"'‘“«]")
PULLQUOTE_WORD = re.compile(r"[^\W_]+", re.UNICODE)
BYLINE_ACRONYMS = {"AFP", "AP", "EFE", "EPA", "FMI", "PA", "NYT", "WSJ"}
BYLINE_PARTICLES = {
    "AND": "and", "DA": "da", "DAS": "das", "DE": "de", "DEL": "del",
    "DO": "do", "DOS": "dos", "E": "e", "LA": "la", "LAS": "las",
    "LOS": "los", "VAN": "van", "VON": "von", "Y": "y",
}

READER_CSS = b"""/* Pressko KOReader stylesheet v5; deliberately device-neutral. */
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  padding: 0;
  line-height: 1.27;
  orphans: 2;
  widows: 2;
}
.article { margin: 0; padding: 0; }
.article + .article { margin-top: 1.4em; }
.article-header {
  margin: 0 0 0.65em;
  padding: 0;
  break-after: avoid;
  page-break-after: avoid;
}
.article-header + * {
  break-before: avoid;
  page-break-before: avoid;
}
h1 {
  margin: 0 0 0.3em;
  padding: 0;
  font-size: 1.65em;
  line-height: 1.08;
  font-weight: bold;
  text-align: left;
  break-inside: avoid;
  page-break-inside: avoid;
  break-after: avoid;
  page-break-after: avoid;
}
h2 {
  margin: 0.25em 0 0.45em;
  padding: 0;
  font-size: 0.92em;
  line-height: 1.3;
  font-weight: normal;
  text-align: left;
  break-inside: avoid;
  page-break-inside: avoid;
  break-after: avoid;
  page-break-after: avoid;
}
.article-header.title-from-subtitle h2 {
  margin-top: 0;
  font-size: 1.45em;
  line-height: 1.1;
  font-weight: bold;
}
p { margin: 0 0 0.16em; padding: 0; }
.byline {
  margin: 0.35em 0 0;
  font-size: 0.78em;
  font-weight: normal;
  letter-spacing: 0.01em;
  text-align: left;
}
.standfirst {
  margin: 0.4em 0 0.55em;
  padding: 0.15em 0 0.15em 0.75em;
  border-left: 0.18em solid currentColor;
  font-size: 1.02em;
  line-height: 1.3;
  font-style: italic;
}
.media {
  display: block;
  max-width: 24em;
  margin: 0.4em auto 0.5em;
  padding: 0;
  text-align: center;
  break-inside: avoid;
  page-break-inside: avoid;
}
.media img,
body > img {
  display: block;
  width: auto;
  height: auto;
  max-width: 100%;
  margin: 0 auto;
  padding: 0;
  object-fit: contain;
}
.media.has-legend img {
  break-after: avoid;
  page-break-after: avoid;
}
.legend {
  display: block;
  margin: 0.16em auto 0;
  padding: 0;
  break-before: avoid;
  page-break-before: avoid;
}
.legend.short { max-width: 14em; }
.legend.medium { max-width: 18em; }
.legend.long { max-width: 24em; }
.image-credit,
.caption {
  margin: 0;
  font-size: 0.78em;
  line-height: 1.3;
  font-style: normal;
  text-align: center;
}
.legend.long .caption { text-align: left; }
.image-credit { margin-top: 0.12em; font-size: 0.66em; text-transform: uppercase; }
.toc h1 { margin-bottom: 0.35em; font-size: 1.35em; }
.toc ol { margin: 0; padding-left: 1.15em; }
.toc ol ol { padding-left: 0.9em; }
.toc li { margin: 0.1em 0; line-height: 1.15; }
a { color: inherit; text-decoration: underline; }
"""

ET.register_namespace("", XHTML_NS)

@dataclass(frozen=True)
class CleanupStats:
    articles_found: int
    articles_kept: int
    duplicates_removed: int
    page_documents_removed: int
    assets_removed: int
    pullquote_elements_removed: int
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


def _pullquote_normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.replace("\u00ad", "")).casefold()
    return " ".join(PULLQUOTE_WORD.findall(value))


def _pullquote_is_all_caps(value: str) -> bool:
    letters = [character for character in value if character.isalpha()]
    return bool(letters) and len(letters) >= 5 and (
        sum(character.isupper() for character in letters) / len(letters) >= 0.82
    )


def _pullquote_is_title_case(value: str) -> bool:
    words = [
        word for word in re.findall(r"[^\W\d_]+", value, re.UNICODE)
        if len(word) > 1
    ]
    if len(words) < 5:
        return False
    return sum(word[0].isupper() for word in words) / len(words) >= 0.65


def _pullquote_body_paragraphs(article: ET.Element) -> list[ET.Element]:
    parents = {child: parent for parent in article.iter() for child in parent}
    children = list(article)
    header = children[0] if children and (
        "article-header" in _classes(children[0])
        or any(_local_name(item.tag) in {"h1", "h2"} for item in children[0].iter())
    ) else None
    media = {
        item for item in article.iter()
        if "media" in _classes(item)
        or (
            _local_name(item.tag) == "div"
            and any(_local_name(child.tag) == "img" for child in item.iter())
        )
    }
    result: list[ET.Element] = []
    for item in article.iter():
        if _local_name(item.tag) != "p":
            continue
        current: ET.Element | None = item
        excluded = False
        while current is not None and current is not article:
            if (
                current is header
                or current in media
                or _classes(current) & PULLQUOTE_EXCLUDED_CLASSES
            ):
                excluded = True
                break
            current = parents.get(current)
        if not excluded and _text(item):
            result.append(item)
    return result


def _pullquote_candidates(paragraphs: list[ET.Element]) -> set[ET.Element]:
    values = [_text(item) for item in paragraphs]
    normalized = [_pullquote_normalize(value) for value in values]
    candidates: set[ET.Element] = set()
    for index, value in enumerate(values):
        words = normalized[index].split()
        if not (8 <= len(words) <= 45 and 45 <= len(value) <= 300):
            continue
        quoted = bool(PULLQUOTE_OPENING_QUOTE.match(value))
        if not value[0].isupper() and not quoted:
            continue
        if _pullquote_is_all_caps(value):
            continue
        if not quoted and not PULLQUOTE_SENTENCE_END.search(value):
            continue
        if not quoted and _pullquote_is_title_case(value):
            continue
        if any(
            index != other_index
            and normalized[index] in other
            and len(other.split()) >= len(words) + 4
            for other_index, other in enumerate(normalized)
        ):
            candidates.add(paragraphs[index])
    return candidates


def _pullquote_looks_like_attribution(value: str) -> bool:
    if len(value) > 180:
        return False
    letters = [character for character in value if character.isalpha()]
    return bool(letters) and (
        sum(character.isupper() for character in letters) / len(letters) >= 0.45
    )


def _remove_redundant_pullquotes(article: ET.Element) -> int:
    """Remove only callouts proven to repeat a longer paragraph in *article*.

    PressReader commonly emits the same print pull quote in a header annotation
    and as one or more ordinary body paragraphs. Exact annotation/body blocks
    are removed atomically so speaker attributions are not left behind.
    """
    paragraphs = _pullquote_body_paragraphs(article)
    candidates = _pullquote_candidates(paragraphs)
    if not candidates:
        return 0

    parents = {child: parent for parent in article.iter() for child in parent}
    children = list(article)
    header = children[0] if children and (
        "article-header" in _classes(children[0])
        or any(_local_name(item.tag) in {"h1", "h2"} for item in children[0].iter())
    ) else None
    annotations = [
        item for item in (header.iter() if header is not None else [])
        if _local_name(item.tag) == "blockquote"
        or _classes(item) & {"annotation", "standfirst"}
    ]
    values = [_text(item) for item in paragraphs]
    body_removals: set[ET.Element] = set()
    annotation_removals: set[ET.Element] = set()

    for annotation in annotations:
        target = _pullquote_normalize(_text(annotation))
        if not target:
            continue
        match: tuple[int, int] | None = None
        for start in range(len(paragraphs)):
            includes_candidate = False
            for end in range(start, min(len(paragraphs), start + 10)):
                includes_candidate = includes_candidate or paragraphs[end] in candidates
                combined = _pullquote_normalize(" ".join(values[start:end + 1]))
                if combined == target and includes_candidate:
                    match = (start, end)
                    break
                if len(combined) > len(target):
                    break
            if match:
                break
        if match:
            annotation_removals.add(annotation)
            body_removals.update(paragraphs[match[0]:match[1] + 1])

    for index, candidate in enumerate(paragraphs):
        if candidate not in candidates or candidate in body_removals:
            continue
        following = values[index + 1] if index + 1 < len(values) else ""
        if following and _pullquote_looks_like_attribution(following):
            # Without an exact annotation/body relationship, keep the pair.
            continue
        body_removals.add(candidate)

    removed = 0
    for item in annotation_removals | body_removals:
        parent = parents.get(item)
        if parent is not None and item in list(parent):
            parent.remove(item)
            removed += 1
    return removed


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
        semantic_class = ""
        if "art-cnt" in classes:
            semantic_class = "article"
        elif "art-title-area" in classes:
            semantic_class = "article-header"
        elif "img-art" in classes:
            semantic_class = "media"
        elif "byline" in classes:
            semantic_class = "byline"
        elif "annotation" in classes:
            semantic_class = "standfirst"
        elif "img-byline" in classes:
            semantic_class = "image-credit"
        elif "img-text" in classes:
            semantic_class = "caption"
        if semantic_class:
            element.set("class", semantic_class)
        else:
            element.attrib.pop("class", None)
        element.attrib.pop("style", None)
        element.attrib.pop("width", None)
        element.attrib.pop("height", None)
        if element.text:
            element.text = element.text.replace("\u00ad", "")
        if element.tail:
            element.tail = element.tail.replace("\u00ad", "")
    _mark_subtitle_only_headers(root)
    _normalize_byline_case(root)
    _normalize_media_markup(root)


def _mark_subtitle_only_headers(root: ET.Element) -> None:
    for header in root.iter():
        if "article-header" not in _classes(header):
            continue
        child_tags = {_local_name(item.tag) for item in header}
        header_classes = ["article-header"]
        if "h1" not in child_tags and "h2" in child_tags:
            header_classes.append("title-from-subtitle")
        header.set("class", " ".join(header_classes))


def _normalize_byline_case(root: ET.Element) -> None:
    """Calm publisher-supplied all-caps names without damaging mixed-case text."""
    word_pattern = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.UNICODE)

    def normal_case(match: re.Match[str]) -> str:
        word = match.group(0)
        letters = "".join(character for character in word if character.isalpha())
        if len(letters) < 2 or not letters.isupper():
            return word
        if word in BYLINE_ACRONYMS:
            return word
        if word == "BY":
            return "By" if not match.string[:match.start()].strip() else "by"
        if word in BYLINE_PARTICLES:
            return BYLINE_PARTICLES[word]
        result = word.lower().title()
        if result.startswith("Mc") and len(result) > 2:
            result = result[:2] + result[2].upper() + result[3:]
        return result

    for element in root.iter():
        if "byline" not in _classes(element):
            continue
        if element.text:
            element.text = re.sub(r"^\s*[|•·]\s*", "", element.text)
            element.text = word_pattern.sub(normal_case, element.text)
        for child in element.iter():
            if child is not element and child.text:
                child.text = word_pattern.sub(normal_case, child.text)
            if child.tail:
                child.tail = word_pattern.sub(normal_case, child.tail)


def _normalize_media_markup(root: ET.Element) -> None:
    """Keep each editorial image and its complete legend in one ordered block."""
    for media in root.iter():
        if "media" not in _classes(media):
            continue
        children = list(media)
        images = [item for item in children if _local_name(item.tag) == "img"]
        if not images:
            continue
        legends = [item for item in children if _has_class(item, "legend")]
        loose_text = [
            item for item in children
            if _has_class(item, "caption") or _has_class(item, "image-credit")
        ]
        if loose_text:
            legend = ET.Element(f"{{{XHTML_NS}}}div", {"class": "legend"})
            # Captions describe the image; the usually shorter source credit
            # follows them. PressReader commonly supplies these in reverse.
            ordered = sorted(
                loose_text,
                key=lambda item: 1 if _has_class(item, "image-credit") else 0,
            )
            for item in ordered:
                media.remove(item)
                legend.append(item)
            image_position = list(media).index(images[-1])
            media.insert(image_position + 1, legend)
            legends.append(legend)
        media_classes = ["media"]
        if legends:
            media_classes.append("has-legend")
            legend_length = len(_text(legends[0]))
            if legend_length <= 80:
                legend_size = "short"
            elif legend_length <= 180:
                legend_size = "medium"
            else:
                legend_size = "long"
            legends[0].set("class", f"legend {legend_size}")
        media.set("class", " ".join(media_classes))


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
<body class="toc"><h1>Contents</h1><ol>{_navigation_html(nodes, toc_path)}</ol></body></html>"""
    return document.encode("utf-8")


def _stylesheet_path(opf_path: str) -> str:
    return posixpath.join(posixpath.dirname(opf_path), READER_STYLE_NAME)


def _add_stylesheet_link(root: ET.Element, document: str, stylesheet: str) -> None:
    head = next((item for item in root.iter() if _local_name(item.tag) == "head"), None)
    if head is None:
        return
    relative = posixpath.relpath(stylesheet, posixpath.dirname(document))
    for item in list(head):
        if _local_name(item.tag) == "link" and "stylesheet" in item.get("rel", "").casefold():
            head.remove(item)
    ET.SubElement(head, f"{{{XHTML_NS}}}link", {
        "rel": "stylesheet", "type": "text/css", "href": relative,
    })


def _decorate_clean_markup(root: ET.Element) -> int:
    """Add stable semantic hooks to an EPUB previously cleaned by Pressko."""
    body = next((item for item in root.iter() if _local_name(item.tag) == "body"), None)
    if body is None:
        return 0
    if any(_local_name(item.tag) == "ol" for item in body):
        body.set("class", "toc")
    for article in list(body):
        if _local_name(article.tag) != "div" or not article.get("id"):
            continue
        article.set("class", "article")
        children = list(article)
        if children and _local_name(children[0].tag) == "div" and any(
            _local_name(item.tag) in {"h1", "h2"} for item in children[0].iter()
        ):
            children[0].set("class", "article-header")
            for item in children[0]:
                if _local_name(item.tag) == "p":
                    item.set("class", "byline")
                elif _local_name(item.tag) == "blockquote":
                    item.set("class", "standfirst")
        for item in article.iter():
            if _local_name(item.tag) == "blockquote":
                item.set("class", "standfirst")
        for media in article.iter():
            if _local_name(media.tag) != "div" or not any(
                _local_name(item.tag) == "img" for item in media
            ):
                continue
            media.set("class", "media")
            image_index = next(
                (index for index, item in enumerate(media) if _local_name(item.tag) == "img"), 0
            )
            for index, item in enumerate(media):
                if _local_name(item.tag) == "p":
                    item.set("class", "image-credit" if index < image_index else "caption")
    _mark_subtitle_only_headers(root)
    _normalize_byline_case(root)
    _normalize_media_markup(root)
    return sum(
        _remove_redundant_pullquotes(article)
        for article in list(body)
        if "article" in _classes(article)
    )


def _apply_reader_style(
    files: dict[str, bytes],
    opf_path: str,
    *,
    decorate: bool = False,
    documents: set[str] | None = None,
) -> int:
    stylesheet = _stylesheet_path(opf_path)
    files[stylesheet] = READER_CSS
    opf_root = _parse_xml(files[opf_path], opf_path)
    manifest = opf_root.find(f"{{{OPF_NS}}}manifest")
    if manifest is None:
        raise ValueError("EPUB package is missing its manifest")
    style_item = next(
        (item for item in manifest if _resolve(opf_path, item.get("href", "")) == stylesheet),
        None,
    )
    if style_item is None:
        used_ids = {item.get("id", "") for item in manifest}
        style_id = "pressko-style"
        suffix = 2
        while style_id in used_ids:
            style_id = f"pressko-style-{suffix}"
            suffix += 1
        ET.SubElement(manifest, f"{{{OPF_NS}}}item", {
            "id": style_id,
            "href": posixpath.relpath(stylesheet, posixpath.dirname(opf_path)),
            "media-type": "text/css",
        })
    files[opf_path] = _serialize(opf_root, OPF_NS)

    styled_documents = documents if documents is not None else set(files)
    pullquote_elements_removed = 0
    for document in sorted(
        name for name in styled_documents if name.lower().endswith((".xhtml", ".html"))
    ):
        root = _parse_xml(files[document], document)
        if decorate:
            pullquote_elements_removed += _decorate_clean_markup(root)
        _add_stylesheet_link(root, document, stylesheet)
        files[document] = _serialize(root, XHTML_NS)
    return pullquote_elements_removed


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


def _clean_document(
    document: str,
    root: ET.Element,
    keep: set[str],
) -> tuple[bytes | None, list[Article], int]:
    body = next((item for item in root.iter() if _local_name(item.tag) == "body"), None)
    head = next((item for item in root.iter() if _local_name(item.tag) == "head"), None)
    if body is None or head is None:
        return None, [], 0

    retained: list[Article] = []
    pullquote_elements_removed = 0
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
        pullquote_elements_removed += _remove_redundant_pullquotes(element)
        retained.append(Article(
            document=document, element=element, article_id=article_id,
            title=title, page_number=_page_number(document),
            fingerprint="", unique_key=key,
        ))

    if not retained:
        return None, [], 0
    for child in list(body):
        body.remove(child)
    for article in retained:
        body.append(article.element)
    for child in list(head):
        if _local_name(child.tag) == "link":
            head.remove(child)
    return _serialize(root, XHTML_NS), retained, pullquote_elements_removed


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
    pullquote_elements_removed = 0
    for document in page_documents:
        cleaned, retained, removed = _clean_document(document, parsed[document], keep_keys)
        pullquote_elements_removed += removed
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
    styled_documents = set(cleaned_documents) | {toc_path}
    if cover_document in files:
        styled_documents.add(cover_document)
    _apply_reader_style(files, opf_path, documents=styled_documents)

    always_keep = {"mimetype", "META-INF/container.xml", opf_path, toc_path, ncx_path}
    if cover_document in files:
        always_keep.add(cover_document)
    output_names = always_keep | set(cleaned_documents) | kept_assets | {_stylesheet_path(opf_path)}
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
        assets_removed=removed_assets,
        pullquote_elements_removed=pullquote_elements_removed,
        original_bytes=original_bytes,
        cleaned_bytes=path.stat().st_size,
    )


def style_pressreader_epub(path: Path) -> int:
    """Apply the reader stylesheet to an existing cleaned EPUB atomically."""
    path = Path(path)
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
    pullquote_elements_removed = _apply_reader_style(files, opf_path, decorate=True)

    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", files["mimetype"], compress_type=zipfile.ZIP_STORED)
            for name in sorted(set(files) - {"mimetype"}):
                archive.writestr(name, files[name])
        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise ValueError("Styled file failed ZIP validation")
            _parse_xml(check.read(opf_path), opf_path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return pullquote_elements_removed


def reader_style_is_current(path: Path) -> bool:
    """Return whether *path* contains the exact stylesheet shipped here."""
    try:
        with zipfile.ZipFile(path) as archive:
            files = set(archive.namelist())
            opf_path = _opf_path({
                "META-INF/container.xml": archive.read("META-INF/container.xml")
            })
            stylesheet = _stylesheet_path(opf_path)
            return stylesheet in files and archive.read(stylesheet) == READER_CSS
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ET.ParseError):
        return False


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--style-only", action="store_true",
        help="style an EPUB that was already cleaned instead of cleaning it again",
    )
    parser.add_argument("epub", nargs="+", type=Path)
    args = parser.parse_args()
    for epub in args.epub:
        if args.style_only:
            removed = style_pressreader_epub(epub)
            print(epub, "styled", f"({removed} redundant callout elements removed)")
        else:
            print(epub, clean_pressreader_epub(epub))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
