"""Generate explicit effective extensions, then require review before upgrades."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from md2word.pandoc import PandocRunner
from md2word.resources import ResourceLocator

root = Path(__file__).resolve().parents[1]
runner = PandocRunner()
enabled = set("""
all_symbols_escapable auto_identifiers backtick_code_blocks blank_before_blockquote
blank_before_header escaped_line_breaks fenced_code_blocks fenced_code_attributes
intraword_underscores lists_without_preceding_blankline pipe_tables
raw_html raw_tex startnum strikeout tex_math_dollars tex_math_single_backslash
""".split())
extensions = runner.run(["--list-extensions=markdown"], cwd=root).stdout.decode().splitlines()
known = {line[1:] for line in extensions if line[:1] in ("+", "-")}
if enabled - known:
    raise ValueError(enabled - known)
switches = {name: name in enabled for name in sorted(known)}
reader = "markdown" + "".join(("+" if on else "-") + name for name, on in switches.items())
ast = json.loads(runner.run(["--from", reader, "--to=json"], cwd=root).stdout)
profile = {
    "profile_version": "stage1-v1",
    "engine_version": runner.run(["--version"], cwd=root).stdout.decode().splitlines()[0],
    "reader": reader, "effective_extensions": switches,
    "ast_api_version": ast["pandoc-api-version"],
    "note": "raw nodes retained for stage-02 visible normalization; never directly export untrusted input",
}
(root / "resources/reader-profile.json").write_text(
    json.dumps(profile, ensure_ascii=False, indent=2) + "\n", "utf-8")
print(json.dumps({"engine": profile["engine_version"], "extensions": len(switches),
                  "ast_api": profile["ast_api_version"]}))
