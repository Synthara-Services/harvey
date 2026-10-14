"""`harvey train` writes what it learned and keeps who emails go out from.

Training used to rebuild the whole config: persona "Harvey", provider
Instantly, New York quiet hours, LinkedIn on, no approval step. A re-train
silently changed the sender and the provider of every email.
"""

import copy

import pytest
import yaml

import harvey.trainer as trainer_mod
from harvey.config import BrainConfig, ConfigFileNotFoundError, HarveyConfig
from harvey.paths import PROJECT_ROOT

LEARNED_PRODUCT = {
    "company_name": "Learned Co",
    "product_name": "Learned Product",
    "product_description": "What the site says",
    "pricing": "From 10",
    "key_benefits": ["fast"],
    "tone": "warm, direct",
}
LEARNED_ICP = {
    "industries": ["Tourism"],
    "company_size": "5-50 employees",
    "titles": ["Founder"],
    "geography": ["Albania"],
}
OBJECTIONS = {"too expensive": "Fair, here is why."}

CONFIGURED = {
    "persona": {
        "name": "Jane Roe", "company": "Example Co", "role": "Founder",
        "email": "jane@example.com", "linkedin": "", "tone": "candid",
    },
    "product": {
        "name": "Old", "description": "old", "pricing": "old", "key_benefits": [],
        "objection_responses": {},
        "offer": {"booking_url": "https://calendar.example.com/x", "meeting_duration": "30 minutes"},
    },
    "icp": {
        "industries": ["Old"], "company_size": "1", "titles": ["Old"], "geography": ["Old"],
        "hiring_signals": ["Head of Growth"],
    },
    "channels": {
        "email": {
            "enabled": True, "provider": "smtp", "max_daily_sends": 10,
            "require_approval": True, "signature": "Jane Roe\nFounder, Example Co",
            "opt_out_line": "Not relevant? Say so.",
        },
        "linkedin": {"enabled": False},
    },
    "usage": {
        "max_daily_claude_percent": 60, "heartbeat_interval_minutes": 30,
        "quiet_hours": {"start": "21:00", "end": "08:00", "timezone": "Europe/Tirane"},
    },
    "custom_section": {"kept": True},
}


def shipped_yaml() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))


def build(settings):
    trainer = trainer_mod.Trainer(brain_config=BrainConfig())
    return trainer._build_config(LEARNED_PRODUCT, LEARNED_ICP, OBJECTIONS, settings)


# ── what a training run writes ──


def test_retrain_keeps_sender_channels_and_usage():
    built = build(CONFIGURED)
    assert built["persona"] == CONFIGURED["persona"]
    assert built["channels"] == CONFIGURED["channels"]
    assert built["usage"] == CONFIGURED["usage"]
    assert built["custom_section"] == {"kept": True}
    HarveyConfig(**built)


def test_retrain_replaces_what_it_learned_and_keeps_the_rest():
    built = build(CONFIGURED)
    assert built["product"]["name"] == "Learned Product"
    assert built["product"]["objection_responses"] == OBJECTIONS
    assert built["product"]["offer"] == CONFIGURED["product"]["offer"]
    assert built["icp"]["industries"] == ["Tourism"]
    assert built["icp"]["hiring_signals"] == ["Head of Growth"]


def test_build_leaves_the_settings_it_was_given_untouched():
    before = copy.deepcopy(CONFIGURED)
    build(CONFIGURED)
    assert CONFIGURED == before


def test_template_company_and_tone_are_filled_from_the_site():
    template = shipped_yaml()
    built = build(template)
    assert built["persona"]["company"] == "Learned Co"
    assert built["persona"]["tone"] == "warm, direct"
    # The sender is never invented: name and email stay as configured.
    assert built["persona"]["name"] == template["persona"]["name"]
    assert built["persona"]["email"] == template["persona"]["email"]
    assert built["channels"] == template["channels"]


def test_first_run_without_any_config_uses_the_shipped_defaults():
    built = build({})
    assert built["channels"]["email"]["provider"] == "gmail"
    assert built["channels"]["email"]["require_approval"] is True
    assert built["persona"]["name"] == ""
    assert built["persona"]["email"] == ""
    assert built["persona"]["company"] == "Learned Co"
    HarveyConfig(**built)


def test_defaults_match_the_shipped_harvey_yaml():
    shipped = shipped_yaml()
    assert trainer_mod.DEFAULT_CHANNELS == shipped["channels"]
    assert trainer_mod.DEFAULT_USAGE == shipped["usage"]
    for key, value in trainer_mod.TEMPLATE_PERSONA.items():
        assert shipped["persona"][key] == value


# ── which settings a training run keeps ──


def test_settings_come_from_the_file_being_written(tmp_path):
    out = tmp_path / "harvey.local.yaml"
    out.write_text(yaml.safe_dump(CONFIGURED), encoding="utf-8")
    assert trainer_mod._settings_in_effect(out) == CONFIGURED


def test_settings_fall_back_to_the_config_in_effect(tmp_path, monkeypatch):
    in_effect = tmp_path / "harvey.yaml"
    in_effect.write_text(yaml.safe_dump(CONFIGURED), encoding="utf-8")
    monkeypatch.setattr(trainer_mod, "_find_config_file", lambda: str(in_effect))
    assert trainer_mod._settings_in_effect(tmp_path / "new.yaml") == CONFIGURED


def test_no_config_anywhere_means_defaults(tmp_path, monkeypatch):
    def missing():
        raise ConfigFileNotFoundError("no config")
    monkeypatch.setattr(trainer_mod, "_find_config_file", missing)
    assert trainer_mod._settings_in_effect(tmp_path / "new.yaml") == {}


# ── the full run, offline: no network, no real config, no real skills/ ──


COMPETITION = {
    "competitors": [{
        "name": "Rival", "how_we_win": "speed",
        "their_weakness": "slow", "migration_angle": "easy",
    }],
    "unique_strengths": ["one team"],
    "common_alternatives": ["spreadsheets"],
}


def returns(value):
    async def stub(*_a, **_k):
        return value
    return stub


def offline_trainer(monkeypatch, tmp_path):
    # Every path the trainer derives from the project root lands in tmp_path,
    # so no version of the trainer can write the real skills/ from a test.
    monkeypatch.setattr(trainer_mod, "PROJECT_ROOT", tmp_path)
    trainer = trainer_mod.Trainer(brain_config=BrainConfig())
    monkeypatch.setattr(trainer.state, "init_db", returns(None))
    monkeypatch.setattr(trainer_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    # No config anywhere unless a test writes one: never the repo's own.
    def no_config():
        raise ConfigFileNotFoundError("no config")
    monkeypatch.setattr(trainer_mod, "_find_config_file", no_config)
    # Skills go to a temporary folder, never the real skills/.
    trainer.skills_dir = tmp_path / "skills"
    trainer.skills_dir.mkdir(exist_ok=True)

    class OnePage:
        async def crawl(self, base_url, max_pages=100):
            return {base_url: "Learned Co builds booking sites."}

    monkeypatch.setattr(trainer_mod, "FallbackCrawler", OnePage)
    monkeypatch.setattr(trainer, "_extract_product_info", returns(LEARNED_PRODUCT))
    monkeypatch.setattr(trainer, "_extract_icp", returns(LEARNED_ICP))
    monkeypatch.setattr(trainer, "_extract_competitive_intel", returns(COMPETITION))
    monkeypatch.setattr(trainer, "_generate_objections", returns(OBJECTIONS))
    return trainer


def proposals(tmp_path):
    return sorted(p.name for p in tmp_path.rglob("*.proposed"))


@pytest.mark.asyncio
async def test_first_training_writes_every_file_directly(tmp_path, monkeypatch):
    out = tmp_path / "harvey.local.yaml"
    trainer = offline_trainer(monkeypatch, tmp_path)

    assert await trainer.train("https://example.com", str(out))
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["product"]["name"] == "Learned Product"
    assert "Learned Product" in (tmp_path / "skills" / "product_knowledge.md").read_text(encoding="utf-8")
    assert "Rival" in (tmp_path / "skills" / "competitive_intel.md").read_text(encoding="utf-8")
    assert proposals(tmp_path) == []


@pytest.mark.asyncio
async def test_retrain_proposes_and_leaves_hand_edits_and_comments_alone(tmp_path, monkeypatch, capsys):
    out = tmp_path / "harvey.local.yaml"
    config_text = "# Kept by hand: review the send limit on 2026-10-28.\n" + yaml.safe_dump(
        CONFIGURED, sort_keys=False
    ).replace("  offer:\n", "  # The link goes only into interested replies.\n  offer:\n")
    assert "# The link goes only" in config_text
    out.write_text(config_text, encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "product_knowledge.md").write_text("# Hand-written\nWe are Example Co.\n", encoding="utf-8")
    (skills / "competitive_intel.md").write_text("# Hand-written cards\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in (out, *skills.iterdir())}
    trainer = offline_trainer(monkeypatch, tmp_path)

    assert await trainer.train("https://example.com", str(out))

    # Every existing file is byte for byte as it was, comments included.
    assert {p: p.read_bytes() for p in before} == before
    assert proposals(tmp_path) == [
        "competitive_intel.md.proposed", "harvey.local.yaml.proposed", "product_knowledge.md.proposed",
    ]
    proposed = yaml.safe_load((tmp_path / "harvey.local.yaml.proposed").read_text(encoding="utf-8"))
    assert proposed["product"]["name"] == "Learned Product"
    assert proposed["product"]["offer"] == CONFIGURED["product"]["offer"]
    assert proposed["persona"] == CONFIGURED["persona"]
    assert proposed["channels"] == CONFIGURED["channels"]
    HarveyConfig(**proposed)
    assert "Learned Product" in (skills / "product_knowledge.md.proposed").read_text(encoding="utf-8")

    printed = capsys.readouterr().out
    assert "harvey.local.yaml.proposed" in printed
    assert "product.name" in printed
    assert "Nothing in effect changed" in printed


@pytest.mark.asyncio
async def test_an_unchanged_proposal_is_not_written(tmp_path, monkeypatch):
    out = tmp_path / "harvey.local.yaml"
    assert await offline_trainer(monkeypatch, tmp_path).train("https://example.com", str(out))
    again = offline_trainer(monkeypatch, tmp_path)
    assert await again.train("https://example.com", str(out))
    assert again.written == [] and again.proposed == []
    assert proposals(tmp_path) == []


@pytest.mark.asyncio
async def test_the_setup_template_is_still_trained_into_directly(tmp_path, monkeypatch):
    # The setup wizard trains into the shipped harvey.yaml, which always
    # exists: it is a template, not a hand-edited config.
    template = tmp_path / "harvey.yaml"
    template.write_text(yaml.safe_dump(shipped_yaml(), sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(trainer_mod, "TEMPLATE_CONFIG", template)
    trainer = offline_trainer(monkeypatch, tmp_path)

    assert await trainer.train("https://example.com", str(template))
    assert yaml.safe_load(template.read_text(encoding="utf-8"))["product"]["name"] == "Learned Product"
    assert proposals(tmp_path) == []


def test_proposals_never_reach_git():
    lines = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.proposed" in lines


@pytest.mark.asyncio
async def test_an_unreadable_config_is_never_overwritten(tmp_path, monkeypatch):
    out = tmp_path / "harvey.local.yaml"
    out.write_text("persona: [unclosed", encoding="utf-8")
    trainer = offline_trainer(monkeypatch, tmp_path)

    class NoCrawl:
        def __init__(self, *_a, **_k):
            raise AssertionError("crawled before checking the config")

    monkeypatch.setattr(trainer_mod, "FallbackCrawler", NoCrawl)
    assert await trainer.train("https://example.com", str(out)) is None
    assert out.read_text(encoding="utf-8") == "persona: [unclosed"
