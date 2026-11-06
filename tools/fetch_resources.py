"""Explicit development-time downloads only; application never calls this script."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "resources"

def fetch(url):
    if sys.platform == "win32":
        # Windows curl uses the OS trust store (including managed proxy roots).
        return subprocess.check_output(["curl.exe", "--fail", "--location",
            "--silent", "--show-error", "--retry", "3", "--max-time", "240", url])
    request = urllib.request.Request(url, headers={"User-Agent": "md2word-stage1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()

def main():
    records = []
    downloads = ROOT / "tools/downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/jgm/pandoc/releases/download/3.6.4/pandoc-3.6.4-windows-x86_64.zip"
    archive = downloads / "pandoc-3.6.4.zip"
    if not archive.exists():
        archive.write_bytes(fetch(url))
    data = archive.read_bytes()
    dest = RES / "pandoc"
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for item in z.infolist():
            relative = Path(*Path(item.filename).parts[1:])
            if not relative.parts or item.is_dir():
                continue
            target = (dest / relative).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError("Invalid archive path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(item))
    records.append({"name": "pandoc", "version": "3.6.4", "url": url,
                    "archive_sha256": hashlib.sha256(data).hexdigest(),
                    "exe_sha256": hashlib.sha256((dest / "pandoc.exe").read_bytes()).hexdigest()})
    url = "https://registry.npmjs.org/mathjax/-/mathjax-3.2.2.tgz"
    archive = downloads / "mathjax-3.2.2.tgz"
    if not archive.exists():
        archive.write_bytes(fetch(url))
    data = archive.read_bytes()
    dest = RES / "preview/mathjax"
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for item in tar:
            if not item.isfile():
                continue
            if item.name.startswith("package/es5/"):
                relative = Path(item.name.removeprefix("package/es5/"))
            elif item.name in ("package/LICENSE", "package/package.json"):
                relative = Path(item.name).name
            else:
                continue
            target = (dest / relative).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError("Invalid archive path")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(item) as source:
                target.write_bytes(source.read())
    # Verify npm package integrity, independently fetched registry metadata.
    import base64
    metadata = json.loads(fetch("https://registry.npmjs.org/mathjax/3.2.2"))
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != metadata["dist"]["integrity"]:
        raise ValueError("MathJax npm integrity mismatch")
    records.append({"name": "mathjax", "version": "3.2.2", "url": url,
                    "archive_sha256": hashlib.sha256(data).hexdigest(),
                    "npm_integrity": actual})
    url = "https://registry.npmjs.org/gsap/-/gsap-3.13.0.tgz"
    archive = downloads / "gsap-3.13.0.tgz"
    if not archive.exists():
        archive.write_bytes(fetch(url))
    data = archive.read_bytes()
    metadata = json.loads(fetch("https://registry.npmjs.org/gsap/3.13.0"))
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    expected = next(item for item in json.loads((RES / "vendor-manifest.json").read_text("utf-8")) if item["name"] == "gsap")
    if actual != metadata["dist"]["integrity"] or actual != expected["npm_integrity"]:
        raise ValueError("GSAP npm integrity mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = tar.extractfile("package/dist/gsap.min.js")
        javascript = member.read()
    if hashlib.sha256(javascript).hexdigest() != expected["js_sha256"]:
        raise ValueError("GSAP JavaScript hash mismatch")
    dest = RES / "preview/gsap"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "gsap.min.js").write_bytes(javascript)
    records.append(expected)
    manifest_path = RES / "vendor-manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text("utf-8"))
        if records != previous:
            raise ValueError("Resource hashes differ from locked manifest")
    manifest_path.write_text(json.dumps(records, indent=2) + "\n", "utf-8")
    print(json.dumps(records, indent=2))

if __name__ == "__main__":
    main()
