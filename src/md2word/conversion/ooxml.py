"""Read OOXML independently of python-docx paragraph.text."""
from dataclasses import dataclass
import posixpath
from pathlib import Path
from urllib.parse import unquote, urlsplit
import zipfile
from lxml import etree as ET
from ..domain.models import digest, failure

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
C = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"w": W, "m": M, "r": R}
def q(namespace, name):
    return "{" + namespace + "}" + name

def math_signature(node) -> str:
    """Ignore Word font/control formatting; retain all mathematical structure."""
    def canonical(element):
        if not isinstance(element.tag, str):
            return None
        if element.tag in {q(M, "ctrlPr"), q(W, "rPr")}:
            return None
        return [element.tag, sorted(element.attrib.items()),
                element.text if element.tag == q(M, "t") else None,
                [value for child in element
                 if (value := canonical(child)) is not None]]
    return digest(canonical(node))

def xml_parse(data):
    parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                          huge_tree=False, remove_blank_text=False)
    result = ET.fromstring(data, parser)
    if result.getroottree().docinfo.doctype:
        raise ValueError("DTD is not allowed")
    return result

@dataclass
class Package:
    parts: dict
    relationships: dict
    document: object
    styles: object
    numbering: object

    @classmethod
    def read(cls, path: Path):
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                names = [i.filename for i in infos]
                if (len(set(names)) != len(names) or len(names) > 4096
                        or sum(i.file_size for i in infos) > 128 * 1024 * 1024):
                    raise ValueError("Invalid package size")
                for name in names:
                    if "\\" in name or name.startswith("/") or ".." in name.split("/"):
                        raise ValueError("Invalid ZIP path")
                required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
                            "word/styles.xml", "word/numbering.xml"}
                if not required <= set(names) or archive.testzip():
                    raise ValueError("Missing or corrupt part")
                parts = {name: xml_parse(archive.read(name)) for name in names
                         if name.endswith(".xml") or name.endswith(".rels")}
            content = parts["[Content_Types].xml"]
            if content.tag != q(C, "Types"):
                raise ValueError("Invalid content types")
            overrides = {x.get("PartName"): x.get("ContentType") for x in content
                         if x.tag == q(C, "Override")}
            defaults = {x.get("Extension"): x.get("ContentType") for x in content
                        if x.tag == q(C, "Default")}
            if overrides.get("/word/document.xml") != "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml":
                raise ValueError("Wrong document type")
            for part, kind in (("styles", "styles"), ("numbering", "numbering")):
                if overrides.get("/word/" + part + ".xml") != (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml." + kind + "+xml"):
                    raise ValueError("Wrong supporting part type")
            for name in names:
                if name != "[Content_Types].xml" and not name.endswith("/") and not (
                        "/" + name in overrides or name.rsplit(".", 1)[-1] in defaults):
                    raise ValueError("No content type")
            for part in overrides:
                if not part or not part.startswith("/") or part[1:] not in names:
                    raise ValueError("Dangling content type")
            relationships = {}
            for name, root in parts.items():
                if not name.endswith(".rels"):
                    continue
                if root.tag != q(P, "Relationships"):
                    raise ValueError("Invalid relationships root")
                owner = "" if name == "_rels/.rels" else (
                    name.rsplit("/_rels/", 1)[0] + "/" + name.rsplit("/", 1)[1][:-5])
                if owner and owner not in names:
                    raise ValueError("Missing relationship owner")
                mapping = {}
                for rel in root:
                    rid, target, kind = rel.get("Id"), rel.get("Target"), rel.get("Type")
                    if not rid or rid in mapping or not target or not kind:
                        raise ValueError("Invalid relationship")
                    external = rel.get("TargetMode") == "External"
                    if not external:
                        resolved = posixpath.normpath(posixpath.join(
                            posixpath.dirname(owner), unquote(urlsplit(target).path).lstrip("/")))
                        if target.startswith("/"):
                            resolved = unquote(urlsplit(target).path).lstrip("/")
                        if resolved.startswith("../") or resolved not in names:
                            raise ValueError("Dangling relationship")
                    mapping[rid] = (target, kind, external)
                relationships[owner] = mapping
            root_rels = relationships.get("", {})
            if not any(t == "word/document.xml" and k.endswith("/officeDocument") and not e
                       for t, k, e in root_rels.values()):
                raise ValueError("Missing main document relationship")
            document_rels = relationships.get("word/document.xml", {})
            for expected_part in ("styles", "numbering"):
                matching = [(t, e) for t, k, e in document_rels.values()
                            if k.endswith("/" + expected_part)]
                if len(matching) != 1 or matching[0] != (expected_part + ".xml", False):
                    raise ValueError("Invalid supporting part relationship")
            for owner, root in parts.items():
                if owner.endswith(".rels") or owner == "[Content_Types].xml":
                    continue
                mapping = relationships.get(owner, {})
                for el in root.iter():
                    for key, value in el.attrib.items():
                        if key.startswith("{" + R + "}") and value not in mapping:
                            raise ValueError("Dangling relationship reference")
            document = parts["word/document.xml"]
            styles, numbering = parts["word/styles.xml"], parts["word/numbering.xml"]
            if document.tag != q(W, "document") or document.find(q(W, "body")) is None:
                raise ValueError("Invalid Word document")
            if styles.tag != q(W, "styles") or numbering.tag != q(W, "numbering"):
                raise ValueError("Invalid styles/numbering")
            return cls(parts, relationships, document, styles, numbering)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.XMLSyntaxError, RuntimeError):
            raise failure("DOCX_INVALID", "Word 文件包结构或关系校验失败。",
                          "停止提交文件，保留原文后重新转换。", "export") from None

    def hyperlink(self, node):
        if node.get(q(W, "anchor")) is not None:
            return "#" + node.get(q(W, "anchor")), node.get(q(W, "tooltip"), "")
        rid = node.get(q(R, "id"))
        target, kind, external = self.relationships["word/document.xml"][rid]
        if not external or not kind.endswith("/hyperlink"):
            raise ValueError("Not an external hyperlink relationship")
        return target, node.get(q(W, "tooltip"), "")

def plain_word_text(root) -> str:
    pieces = []
    for el in root.iter():
        if el.tag == q(W, "t"):
            pieces.append(el.text or "")
        elif el.tag in {q(W, "br"), q(W, "cr")}:
            pieces.append("\n")
        elif el.tag == q(W, "tab"):
            pieces.append("\t")
    return "".join(pieces)
