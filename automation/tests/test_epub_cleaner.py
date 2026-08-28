import sys
import tempfile
import unittest
import zipfile
import posixpath
from pathlib import Path
from xml.etree import ElementTree as ET

AUTOMATION = Path(__file__).parents[1]
sys.path.insert(0, str(AUTOMATION))

from epub_cleaner import (
    Article,
    NavigationNode,
    _deduplicate_navigation,
    _mark_subtitle_only_headers,
    _normalize_byline_case,
    clean_pressreader_epub,
    reader_style_is_current,
    style_pressreader_epub,
)


XHTML = "http://www.w3.org/1999/xhtml"


def page(article_id="", title="", body="", image="", short=False):
    if not article_id:
        article = ""
    else:
        paragraphs = f"<p>{body}</p>" if body else "<p>Short</p>"
        picture = f'<div class="img-art"><img src="{image}"/><span class="img-text">Caption</span></div>' if image else ""
        article = f"""<div class="art-cnt" id="{article_id}">
          <div class="legal-header">Repeated legal line</div>
          <div class="art-header"><a href="#top">Page 5</a></div>
          <div class="art-title-area"><img class="art-thumb-img" src="thumb.jpeg"/><div class="title">{title}</div><span class="byline">By Writer</span></div>
          {paragraphs}<div class="art-divider">• Page 6 •</div>{picture}</div>"""
    return f"""<?xml version="1.0"?><html xmlns="{XHTML}"><head><title>Page</title>
      <link rel="stylesheet" href="../css/style.css"/></head><body>
      <div class="page-cnt"><div class="page-header">Page navigation</div><img src="page.jpeg"/></div>
      {article}</body></html>""".encode()


class EpubCleanerTest(unittest.TestCase):
    def test_continuation_articles_have_one_navigation_entry(self):
        element = ET.Element("article")
        first = Article("page-010/page-010.xhtml", element, "art-1", "Long Report", 10, "a", "first")
        continuation = Article("page-012/page-012.xhtml", element, "art-1", "Long Report", 12, "b", "continuation")
        later_story = Article("page-020/page-020.xhtml", element, "art-1", "Long Report", 20, "c", "later")

        nodes = _deduplicate_navigation([
            NavigationNode("Long Report", article=first),
            NavigationNode("Long Report", article=continuation),
            NavigationNode("Long Report", article=later_story),
        ])

        self.assertEqual([node.article.unique_key for node in nodes], ["first", "later"])

    def test_subtitle_only_header_keeps_title_emphasis(self):
        root = ET.fromstring(
            f'<div xmlns="{XHTML}" class="article-header"><h2>Only title</h2></div>'
        )
        _mark_subtitle_only_headers(root)
        self.assertEqual(root.get("class"), "article-header title-from-subtitle")

    def test_uppercase_bylines_are_normalized_safely(self):
        root = ET.fromstring(
            f'<div xmlns="{XHTML}"><p class="byline">'
            '| BY JOHN MCCORMICK AND ZIA UR-REHMAN / AFP writer@example.com'
            '</p></div>'
        )
        _normalize_byline_case(root)
        self.assertEqual(
            "".join(root.itertext()),
            "By John McCormick and Zia Ur-Rehman / AFP writer@example.com",
        )

    def make_epub(self, path):
        long_body = "The same complete article body appears on several physical pages. " * 4
        opf = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
          <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test</dc:title><dc:creator>NewspaperDirect</dc:creator><dc:identifier id="id">x</dc:identifier></metadata>
          <manifest>
          <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
          <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/><item id="cover-image" href="cover.jpeg" media-type="image/jpeg"/>
          <item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/><item id="thumbs" href="thumbnails.xhtml" media-type="application/xhtml+xml"/>
          <item id="css" href="css/style.css" media-type="text/css"/>
          <item id="p1" href="page-001/page-001.xhtml" media-type="application/xhtml+xml"/><item id="p5" href="page-005/page-005.xhtml" media-type="application/xhtml+xml"/>
          <item id="p6" href="page-006/page-006.xhtml" media-type="application/xhtml+xml"/><item id="p7" href="page-007/page-007.xhtml" media-type="application/xhtml+xml"/>
          <item id="page" href="page-005/page.jpeg" media-type="image/jpeg"/><item id="thumb" href="page-005/thumb.jpeg" media-type="image/jpeg"/>
          <item id="photo" href="page-005/art_photo.jpeg" media-type="image/jpeg"/></manifest>
          <spine toc="ncx"><itemref idref="cover"/><itemref idref="toc"/><itemref idref="p1"/><itemref idref="p5"/><itemref idref="p6"/><itemref idref="p7"/></spine></package>"""
        ncx = """<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/><docTitle><text>Test</text></docTitle><navMap>
          <navPoint id="s-europe"><navLabel><text>Europe</text></navLabel><content src="page-005/page-005.xhtml#top"/>
            <navPoint id="page-005"><navLabel><text>Page 5</text></navLabel><content src="page-005/page-005.xhtml"/>
              <navPoint id="old-main"><navLabel><text>Main Story</text></navLabel><content src="page-005/page-005.xhtml#a"/></navPoint>
            </navPoint>
          </navPoint>
          <navPoint id="s-culture"><navLabel><text>Culture</text></navLabel><content src="page-007/page-007.xhtml#top"/>
            <navPoint id="page-007"><navLabel><text>Page 7</text></navLabel><content src="page-007/page-007.xhtml"/>
              <navPoint id="old-cartoon"><navLabel><text>Cartoon</text></navLabel><content src="page-007/page-007.xhtml#short"/></navPoint>
            </navPoint>
          </navPoint>
        </navMap></ncx>"""
        container = """<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
        cover = f"""<?xml version="1.0"?><html xmlns="{XHTML}"><head><title>Cover</title></head><body><img src="cover.jpeg"/></body></html>"""
        files = {
            "mimetype": b"application/epub+zip", "META-INF/container.xml": container.encode(),
            "OEBPS/content.opf": opf.encode(), "OEBPS/toc.ncx": ncx.encode(),
            "OEBPS/toc.xhtml": b"old toc", "OEBPS/thumbnails.xhtml": b"old thumbs",
            "OEBPS/cover.xhtml": cover.encode(), "OEBPS/cover.jpeg": b"cover",
            "OEBPS/css/style.css": b"old", "OEBPS/page-001/page-001.xhtml": page("a", "Preview", long_body),
            "OEBPS/page-005/page-005.xhtml": page("a", "Main Story", long_body, "art_photo.jpeg"),
            "OEBPS/page-006/page-006.xhtml": page("a", "Main Story", long_body, "../page-005/art_photo.jpeg"),
            "OEBPS/page-007/page-007.xhtml": page("short", "Cartoon", "tiny"),
            "OEBPS/page-005/page.jpeg": b"page", "OEBPS/page-005/thumb.jpeg": b"thumb",
            "OEBPS/page-005/art_photo.jpeg": b"photo",
        }
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", files.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
            for name, data in files.items():
                archive.writestr(name, data)

    def test_article_first_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            epub = Path(temp) / "issue.epub"
            self.make_epub(epub)
            stats = clean_pressreader_epub(epub)
            self.assertEqual(stats.articles_found, 4)
            self.assertEqual(stats.articles_kept, 2)
            self.assertEqual(stats.duplicates_removed, 2)
            with zipfile.ZipFile(epub) as archive:
                names = set(archive.namelist())
                self.assertEqual(archive.infolist()[0].filename, "mimetype")
                self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
                self.assertIn("OEBPS/cover.xhtml", names)
                self.assertIn("OEBPS/cover.jpeg", names)
                self.assertIn("OEBPS/page-005/art_photo.jpeg", names)
                self.assertNotIn("OEBPS/page-005/page.jpeg", names)
                self.assertNotIn("OEBPS/page-005/thumb.jpeg", names)
                self.assertNotIn("OEBPS/thumbnails.xhtml", names)
                self.assertNotIn("OEBPS/css/style.css", names)
                self.assertIn("OEBPS/pressko.css", names)
                self.assertNotIn("OEBPS/page-001/page-001.xhtml", names)
                text = "".join(archive.read(name).decode("utf-8", "ignore") for name in names if name.endswith(".xhtml"))
                self.assertNotIn("art-divider", text)
                self.assertNotIn("page-header", text)
                self.assertNotIn("legal-header", text)
                self.assertNotIn("style=", text)
                self.assertIn('class="article"', text)
                self.assertIn('class="article-header"', text)
                self.assertIn('class="media has-legend"', text)
                self.assertIn('class="caption"', text)
                self.assertIn('class="legend short"', text)
                self.assertIn("Main Story", text)
                self.assertIn("Cartoon", text)
                css = archive.read("OEBPS/pressko.css").decode("utf-8")
                self.assertIn("max-width: 100%", css)
                self.assertIn("page-break-before: always", css)
                self.assertNotIn("border-bottom", css)
                self.assertIn("p { margin: 0 0 0.16em", css)
                self.assertIn(".toc li { margin: 0.1em", css)
                self.assertIn(".legend.short { max-width: 14em", css)
                self.assertIn(".legend.medium { max-width: 18em", css)
                self.assertIn(".legend.long { max-width: 24em", css)
                self.assertNotIn("max-height:", css)
                self.assertNotIn("caption-side:", css)
                self.assertIn("text-align: center", css)
                self.assertIn("line-height: 1.27", css)
                self.assertIn("font-size: 0.92em", css)
                self.assertIn("font-size: 1.45em", css)
                self.assertIn("font-size: 0.78em", css)
                self.assertIn("font-weight: normal", css)
                styled_page = archive.read("OEBPS/page-005/page-005.xhtml").decode("utf-8")
                self.assertLess(styled_page.index("art_photo.jpeg"), styled_page.index("Caption"))
                ncx = ET.fromstring(archive.read("OEBPS/toc.ncx"))
                ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
                top_labels = [
                    point.findtext("n:navLabel/n:text", namespaces=ns)
                    for point in ncx.findall("n:navMap/n:navPoint", ns)
                ]
                self.assertEqual(top_labels, ["Europe", "Culture"])
                self.assertEqual(
                    ncx.findtext("n:navMap/n:navPoint/n:navPoint/n:navLabel/n:text", namespaces=ns),
                    "Main Story",
                )
                for name in names:
                    if name.endswith((".opf", ".ncx", ".xhtml")):
                        root = ET.fromstring(archive.read(name))
                        for element in root.iter():
                            for attribute in ("src", "href"):
                                reference = element.get(attribute, "")
                                if not reference or reference.startswith(("#", "http:")):
                                    continue
                                target = posixpath.normpath(posixpath.join(
                                    posixpath.dirname(name), reference.split("#", 1)[0]
                                ))
                                self.assertIn(target, names, f"broken reference from {name}: {reference}")

    def test_style_only_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            epub = Path(temp) / "issue.epub"
            self.make_epub(epub)
            self.assertFalse(reader_style_is_current(epub))
            clean_pressreader_epub(epub)
            self.assertTrue(reader_style_is_current(epub))
            style_pressreader_epub(epub)
            style_pressreader_epub(epub)
            self.assertTrue(reader_style_is_current(epub))
            with zipfile.ZipFile(epub) as archive:
                names = archive.namelist()
                self.assertEqual(names.count("OEBPS/pressko.css"), 1)
                opf = ET.fromstring(archive.read("OEBPS/content.opf"))
                ns = {"o": "http://www.idpf.org/2007/opf"}
                styles = [
                    item for item in opf.findall("o:manifest/o:item", ns)
                    if item.get("media-type") == "text/css"
                ]
                self.assertEqual(len(styles), 1)
                page_text = archive.read("OEBPS/page-005/page-005.xhtml").decode("utf-8")
                self.assertEqual(page_text.count("pressko.css"), 1)


if __name__ == "__main__":
    unittest.main()
