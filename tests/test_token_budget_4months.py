from config import Settings


def test_style_humanizer_defaults_to_low_cost_model() -> None:
    assert Settings.model_fields["openai_model"].default == "gpt-4o-mini"
    assert Settings.model_fields["ai_humanize_model"].default == "gpt-4o-mini"


def test_dialog_brain_keeps_high_quality_model_and_token_headroom() -> None:
    assert Settings.model_fields["ai_brain_model"].default == "gpt-5.4-mini"
    assert Settings.model_fields["ai_brain_max_completion_tokens"].default == 2000


def test_neurobalance_monthly_budget_slice_is_conservative() -> None:
    # $5/month leaves ~$10/month of the shared $15 target for KORGAN initially.
    # Railway may rebalance after real usage telemetry without changing code.
    assert Settings.model_fields["monthly_ai_budget_usd"].default == 5.0


def test_budget_and_model_settings_remain_environment_overridable() -> None:
    fields = Settings.model_fields
    for name in ("monthly_ai_budget_usd", "ai_humanize_model", "ai_brain_model"):
        assert fields[name].validation_alias is not None
