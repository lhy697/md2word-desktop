"""Prepare a controlled base reference from the pinned bundled Pandoc."""
import hashlib
import argparse
import io
import json
from pathlib import Path
import sys
import zipfile
from lxml import etree as ET
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from md2word.pandoc import PandocRunner
folder = ROOT / "resources/templates"
folder.mkdir(exist_ok=True)
parser = argparse.ArgumentParser()
parser.add_argument("--upgrade", action="store_true")
args = parser.parse_args()
original = PandocRunner().run(["--print-default-data-file=reference.docx"], cwd=ROOT).stdout
result = io.BytesIO()
namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
def w(name):
    return "{" + namespace + "}" + name
with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(result, "w") as target:
    for info in source.infolist():
        content = source.read(info.filename)
        if info.filename == "word/styles.xml":
            styles = ET.fromstring(content)
            if not any(x.get(w("styleId")) == "SourceCode" for x in styles):
                style = ET.SubElement(styles, w("style"), {w("type"): "paragraph", w("styleId"): "SourceCode"})
                ET.SubElement(style, w("name"), {w("val"): "Source Code"})
                ET.SubElement(style, w("basedOn"), {w("val"): "Normal"})
                run = ET.SubElement(style, w("rPr"))
                ET.SubElement(run, w("rFonts"), {w("ascii"): "Consolas", w("hAnsi"): "Consolas"})
                ET.SubElement(run, w("sz"), {w("val"): "20"})
            content = ET.tostring(styles, xml_declaration=True, encoding="UTF-8")
        target.writestr(info, content)
data = result.getvalue()
path = folder / "reference-base.docx"
if path.exists():
    if path.read_bytes() != data and not args.upgrade:
        raise ValueError("Reference differs; review before replacing")
path.write_bytes(data)
manifest = {"template_version": "base-pandoc-3.6.4-v2",
            "engine": "pandoc.exe 3.6.4",
            "source": "bundled Pandoc default reference + explicit SourceCode paragraph style",
            "sha256": hashlib.sha256(data).hexdigest()}
(folder / "reference-base.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
print(json.dumps(manifest))
