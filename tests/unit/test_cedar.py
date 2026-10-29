"""The Gateway's Cedar policies are rendered from the same persona `sources`
fields the agent enforces (dual allowlist, one definition): who the
agent lets call a source is exactly who the Gateway permits."""

import pytest

from data_agent.governance import cedar, policies

ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/gw-abc"
TOOLS = {"labels-api": ["screen_addresses", "label_lookup"], "redshift": ["cost_total"]}


def _callers(rendered):
    return {p.name: p.description for p in rendered}


def test_the_shipped_personas_render_the_same_allowlist_the_agent_enforces():
    rendered = cedar.render(policies.POLICIES, TOOLS, ARN, ("custom:tenant", "custom:role"))
    by_action = {p.statement.split('Action::"')[1].split('"')[0]: p for p in rendered}
    assert set(by_action) == {
        "labels-api___screen_addresses",
        "labels-api___label_lookup",
        "redshift___cost_total",
    }
    for action, pol in by_action.items():
        target = action.split("___")[0]
        for persona, policy in policies.POLICIES.items():
            if ":" not in persona:
                continue
            tenant, _, role = persona.partition(":")
            agent_side = target in cedar.allowed_targets(policy, sorted(TOOLS))
            rendered_in = (
                f'getTag("custom:tenant") == "{tenant}" && principal.hasTag("custom:role") '
                f'&& principal.getTag("custom:role") == "{role}")'
            ) in pol.statement
            assert agent_side == rendered_in, (action, persona)
    # the concrete matrix today: secops / finops may NOT screen labels
    labels = by_action["labels-api___screen_addresses"].statement
    assert '"investigator"' in labels and '"junior"' in labels and '"admin"' in labels
    assert '"secops"' not in labels and '"finops"' not in labels


def test_a_target_no_persona_lists_gets_no_permit_at_all():
    personas = {"t:r": {"tables": {"x": {}}, "sources": ["labels-api"]}}
    rendered = cedar.render(personas, TOOLS, ARN, ("custom:tenant", "custom:role"))
    assert all("redshift" not in p.statement for p in rendered)  # deny-by-default
    assert len(rendered) == 2


def test_deny_all_and_restricted_without_sources_reach_nothing():
    assert cedar.allowed_targets({"deny_all": True}, ["a"]) == []
    assert cedar.allowed_targets({"tables": {"x": {}}}, ["a"]) == []  # restricted, no field
    assert cedar.allowed_targets({"allow_raw_paths": True}, ["b", "a"]) == ["a", "b"]  # admin
    assert cedar.allowed_targets({"allow_raw_paths": True, "sources": []}, ["a"]) == []


def test_every_policy_names_the_exact_gateway_and_a_valid_name():
    for p in cedar.render(policies.POLICIES, TOOLS, ARN, ("custom:tenant", "custom:role")):
        assert f'resource == AgentCore::Gateway::"{ARN}"' in p.statement
        assert p.name.startswith(cedar.NAME_PREFIX) and len(p.name) <= cedar.MAX_NAME
        assert p.name.replace("_", "").isalnum()


@pytest.mark.parametrize("bad", ['x"; permit(', "a b", "x\nforbid"])
def test_values_that_would_need_escaping_are_refused(bad):
    with pytest.raises(ValueError, match="refusing to render"):
        cedar.render({f"t:{bad}": {"sources": ["labels-api"]}}, TOOLS, ARN, ("c:t", "c:r"))


def test_claim_names_follow_the_idp_claim_path_env(monkeypatch):
    monkeypatch.setenv("CLAIM_TENANT_PATH", "org")
    monkeypatch.setenv("CLAIM_ROLE_PATH", "grp")
    assert cedar.claim_names() == ("org", "grp")
    monkeypatch.delenv("CLAIM_TENANT_PATH")
    monkeypatch.delenv("CLAIM_ROLE_PATH")
    assert cedar.claim_names() == ("custom:tenant", "custom:role")
