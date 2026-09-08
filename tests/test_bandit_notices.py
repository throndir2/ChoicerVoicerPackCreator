from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "choicer_voicer_pack_creator"
RESOURCES = PACKAGE / "resources"


def test_bandit_source_license_is_preserved_separately_from_model_terms():
    original = (PACKAGE / "_bandit" / "LICENSE").read_text(encoding="utf-8")
    copied = (RESOURCES / "BandIt-Apache-2.0.txt").read_text(encoding="utf-8")
    assert copied == original
    assert "Apache License" in copied
    assert "Version 2.0, January 2004" in copied

    weights = (RESOURCES / "BandIt-CC-BY-NC-4.0.txt").read_text(encoding="utf-8")
    assert "Attribution-NonCommercial 4.0 International" in weights
    assert "Section 8 -- Interpretation" in weights
    assert len(weights) > 18000


def test_bandit_attribution_identifies_exact_combined_weights_and_creators():
    attribution = (RESOURCES / "BandIt-Attribution.txt").read_text(encoding="utf-8")
    for expected in (
        "Karn N. Watcharasupat",
        "Chih-Wei Wu",
        "Iroro Orife",
        "bandit-combined.ckpt",
        "446680129",
        "ebcd8a3c8c783aa8f3379c0cab925b76987f4f8959dfb595a84426817e1ffb60",
        "d04760e77bb947668d8f5582d36b45a0",
        "https://zenodo.org/records/13327983",
        "https://creativecommons.org/licenses/by-nc/4.0/",
        "d5563d9031e95fdaa3e5a73d5020b9a0df61adb6",
        "not covered by the application's MIT license",
        "do not endorse this application",
    ):
        assert expected in attribution
