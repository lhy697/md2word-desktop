"""Create the one built-in lecture reference from the controlled stage-03 base."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile
from lxml import etree as ET
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from md2word.templates.service import resolve_style
from md2word.templates.word_styles import apply_page, apply_styles
from md2word.conversion.ooxml import NS

def main():
    args = argparse.ArgumentParser()
    args.add_argument("--upgrade", action="store_true")
    options = args.parse_args()
    style = resolve_style()
    folder = ROOT / "resources/templates"
    target = folder / "lecture-reference.docx"
    temp = folder / ".lecture-reference.build.docx"
    if temp.exists():
        raise FileExistsError("Reference build file exists")
    try:
        with zipfile.ZipFile(folder / "reference-base.docx") as source, zipfile.ZipFile(temp, "x") as output:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "word/styles.xml":
                    tree = ET.fromstring(data)
                    apply_styles(tree, style.config)
                    data = ET.tostring(tree, encoding="UTF-8", xml_declaration=True)
                elif info.filename == "word/document.xml":
                    tree = ET.fromstring(data)
                    for section in tree.findall(".//w:sectPr", NS):
                        apply_page(section, style.config.page)
                    data = ET.tostring(tree, encoding="UTF-8", xml_declaration=True)
                output.writestr(info, data)
        data = temp.read_bytes()
        if target.exists() and target.read_bytes() != data and not options.upgrade:
            raise ValueError("Reference changed; review and use --upgrade")
        target.write_bytes(data)
        manifest = {
            "template_version": "lecture-v1", "engine": "pandoc.exe 3.6.4",
            "source": "controlled stage-03 base plus immutable lecture style",
            "style_hash": style.resolved_hash, "sha256": hashlib.sha256(data).hexdigest()}
        (folder / "lecture-reference.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
        print(json.dumps(manifest))
    finally:
        temp.unlink(missing_ok=True)

if __name__ == "__main__":
    main()

