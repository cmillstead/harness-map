from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]

def test_no_vault_output_path_in_skill_or_template():
    for name in ("SKILL.md", "report-template.md"):
        text = (SKILL_DIR / name).read_text()
        assert "obsidian-vault/AI/output" not in text, f"{name} still references the vault output dir"

def test_civc_reference_vendored_and_referenced():
    civc = SKILL_DIR / "civc-reference.md"
    assert civc.is_file(), "civc-reference.md not vendored"
    body = civc.read_text()
    assert "Afford" in body and "covered" in body and "empty" in body
    skill = (SKILL_DIR / "SKILL.md").read_text()
    assert "civc-reference.md" in skill
    assert "kb/Docs/harness-inventory" not in skill, "SKILL.md still points at the vault KB path"

def test_out_dir_convention_present():
    skill = (SKILL_DIR / "SKILL.md").read_text()
    assert "harness-map-reports" in skill
    assert "mkdir -p" in skill

def test_skill_md_under_200_lines():
    n = len((SKILL_DIR / "SKILL.md").read_text().splitlines())
    assert n <= 200, f"SKILL.md is {n} lines (write-guard cap is 200)"

def test_all_eight_headline_metrics_have_footnotes():
    tmpl = (SKILL_DIR / "report-template.md").read_text()
    for slug in ("words", "tokens", "filecount", "dup", "binary", "over200", "orphanreg", "orphanscript"):
        assert f"[^{slug}]:" in tmpl, f"missing footnote definition [^{slug}]"
