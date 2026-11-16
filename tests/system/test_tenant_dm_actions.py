"""Owner, 2026-11-14: a DM button pressed by an additional wearer goes to that wearer's runtime, never to the owner's facade."""

from __future__ import annotations

from types import SimpleNamespace

from application.models import OutgoingMessage
from system.tenant_dm_actions import NOT_YET, TenantAwareActions
from tenancy.router import TenantRouter
from tests.system.test_tenant_gateway import OWNER_DISCORD, g  # noqa: F401


class Facade:
    def __init__(self, name) -> None:
        self.name, self.presses = name, []

    def press(self, *, discord_user_id, action, ident):
        self.presses.append((discord_user_id, action, ident))
        return OutgoingMessage(text=self.name)


def test_presses_go_to_the_presser_runtime(g) -> None:  # noqa: F811
    alex = g.member("2000002", "Alex")
    g.member("3000003", "Bo")
    owner = Facade("owner")
    actions = TenantAwareActions({"recovery": owner}, router=TenantRouter(g.registry), supervisor=g.supervisor)
    alex_facade = Facade("alex")
    g.supervisor.runtimes[alex.id] = SimpleNamespace(tenant_id=alex.id, dm_actions={"recovery": alex_facade})
    press = lambda who: actions.get("recovery").press(discord_user_id=who, action="approve", ident="x").text  # noqa: E731
    assert press(OWNER_DISCORD) == "owner" and press("9999999") == "owner"          # the owner, or a stranger (the owner's facade refuses them)
    assert press("2000002") == "alex" and alex_facade.presses == [("2000002", "approve", "x")]
    assert press("3000003") == NOT_YET.text                                          # Bo has no runtime facade for it (recovery tasks stay owner-only)
    assert owner.presses == [(OWNER_DISCORD, "approve", "x"), ("9999999", "approve", "x")]
    assert bool(actions) and actions["recovery"] is owner
