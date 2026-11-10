from fastapi import APIRouter

from dbarena.env.interface import action_specs, observation_space_catalog


def build_interface_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["interface"])

    @router.get("/interface")
    def get_interface():
        """Self-describing environment interface: episode action specs and the observation catalog."""
        return {"actions": action_specs(), "observations": observation_space_catalog()}

    return router
