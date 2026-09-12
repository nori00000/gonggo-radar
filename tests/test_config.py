"""Tests for alert.config -- YAML loading and knowledge config."""

from alert.config import get_config, KnowledgeConfig, ObsidianConfig


class TestConfigLoads:
    """Tests for basic config loading."""

    def test_config_loads_successfully(self):
        """get_config() should return AppConfig without errors."""
        cfg = get_config(reload=True)
        assert cfg is not None
        assert cfg.name == "농업공고알림봇"

    def test_config_has_knowledge_section(self):
        """Config should have knowledge layer settings."""
        cfg = get_config(reload=True)
        assert hasattr(cfg, "knowledge")
        assert isinstance(cfg.knowledge, KnowledgeConfig)


class TestKnowledgeConfigDefaults:
    """Tests for knowledge config default values."""

    def test_knowledge_enabled_by_default(self):
        """Knowledge layer should be enabled by default in config.yaml."""
        cfg = get_config(reload=True)
        assert cfg.knowledge.enabled is True

    def test_classifier_defaults(self):
        """Classifier should have keyword method by default."""
        cfg = get_config(reload=True)
        assert cfg.knowledge.classifier.enabled is True
        assert cfg.knowledge.classifier.method == "keyword"

    def test_vector_defaults(self):
        """Vector config should use text-embedding-3-small."""
        cfg = get_config(reload=True)
        assert cfg.knowledge.vector.embedding_model == "text-embedding-3-small"
        assert cfg.knowledge.vector.dimensions == 256

    def test_n8n_disabled_by_default(self):
        """n8n should be disabled by default."""
        cfg = get_config(reload=True)
        assert cfg.knowledge.n8n.enabled is False


class TestObsidianAuthor:
    """Tests for configurable author field."""

    def test_author_from_yaml(self):
        """Author field should be loaded from config.yaml."""
        cfg = get_config(reload=True)
        assert cfg.knowledge.obsidian.author == "이상민"

    def test_obsidian_config_has_author_field(self):
        """ObsidianConfig dataclass should have author field."""
        oc = ObsidianConfig()
        assert hasattr(oc, "author")
        assert oc.author == "이상민"  # default value


def test_source_config_bypass_threshold_default_false():
    """SourceConfig.bypass_threshold 기본값은 False."""
    from alert.config import SourceConfig

    assert SourceConfig().bypass_threshold is False
    assert SourceConfig(bypass_threshold=True).bypass_threshold is True


def test_config_yaml_declares_bypass_sources():
    """계약 v1.2: kofpi/forest_press/lawmaking/coop은 bypass_threshold=true."""
    from pathlib import Path

    import yaml

    config_path = Path(__file__).resolve().parent.parent / "alert" / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    sources = raw["crawler"]["sources"]

    for name in ("kofpi", "forest_press", "lawmaking", "coop"):
        assert sources[name].get("bypass_threshold") is True, name
