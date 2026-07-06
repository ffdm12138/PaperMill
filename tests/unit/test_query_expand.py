import pytest

from src.discovery.query_expand import expand_query


def test_expand_query_adds_translations_and_domain_terms():
    expanded = expand_query("风吹雪 升华 破碎", domain_id="blowing_snow_physics")
    assert expanded["original_query"] == "风吹雪 升华 破碎"
    assert "blowing snow" in expanded["terms"]
    assert "sublimation" in expanded["terms"]
    assert len(expanded["expanded_queries"]) >= 2


def test_expand_query_rejects_empty_and_invalid_domain():
    with pytest.raises(ValueError):
        expand_query("")
    with pytest.raises(ValueError):
        expand_query("snow", domain_id="bad_domain")


def test_expand_query_splash_and_particle_size():
    expanded = expand_query("击溅 粒径分布")
    assert "splash" in expanded["terms"]
    assert "particle size distribution" in expanded["terms"]
    assert len(expanded["expanded_queries"]) >= 2


def test_expand_query_friction_velocity_and_boundary_layer():
    expanded = expand_query("摩阻风速 地表剪切力 大气边界层")
    assert "friction velocity" in expanded["terms"]
    assert "surface shear stress" in expanded["terms"]
    assert "atmospheric boundary layer" in expanded["terms"]

