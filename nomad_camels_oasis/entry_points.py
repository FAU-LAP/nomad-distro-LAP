"""NOMAD plugin entry point for the CAMELS bridge."""

from __future__ import annotations

import os

from pydantic import Field

from nomad.config.models.plugins import (
    APIEntryPoint,
    ActionEntryPoint,
    DashboardEntryPoint,
    SchemaPackageEntryPoint,
)


class CAMELSAPIEntryPoint(APIEntryPoint):
    """Configuration for the OASIS-side CAMELS API proxy."""

    camels_base_url: str = Field(
        "http://camels:8000",
        description="Base URL of the CAMELS FastAPI service reachable by OASIS."
    )
    request_timeout_seconds: float = Field(10, gt=0, le=120)
    settings_path: str = Field("/app/.volumes/fs/camels-bridge/settings.json")

    def dict_safe(self):
        """Expose the FastAPI documentation URL to the GUI v2 API catalogue.

        ``APIEntryPoint.dict_safe`` deliberately exports only the standard
        plugin fields.  GUI v2 enables its documentation button only when the
        catalogue item contains ``documentation_absolute_url``.  The CAMELS
        proxy already serves Swagger UI at ``<api_base_path>/camels/docs``;
        publish that same deployment-relative URL here.
        """
        from nomad.config import config

        result = super().dict_safe()
        result["documentation_absolute_url"] = (
            f"{config.services.api_base_path.rstrip('/')}/camels/docs"
        )
        return result

    def load(self):
        from nomad_camels_oasis.app import create_app

        return create_app(
            camels_base_url=self.camels_base_url,
            api_key=os.environ.get("CAMELS_API_KEY"),
            timeout_seconds=self.request_timeout_seconds,
            settings_path=self.settings_path,
            encryption_secret=os.environ.get("NOMAD_SERVICES_API_SECRET"),
        )


camels = CAMELSAPIEntryPoint(
    name="CAMELS remote control",
    description="Authenticated OASIS backend proxy for selected CAMELS protocols.",
    prefix="camels",
)


class CAMELSMeasurementSchemaEntryPoint(SchemaPackageEntryPoint):
    def load(self):
        from nomad_camels_oasis.measurement_schema import m_package

        return m_package


class CAMELSMeasurementActionEntryPoint(ActionEntryPoint):
    """Register the CPU workflow that starts and monitors CAMELS runs."""

    def load(self):
        from nomad.actions import Action, TaskQueue
        from nomad_camels_oasis.actions.activities import (
            get_camels_run_status,
            start_camels_run,
        )
        from nomad_camels_oasis.actions.workflows import CAMELSMeasurementWorkflow

        return Action(
            task_queue=TaskQueue.CPU,
            workflow=CAMELSMeasurementWorkflow,
            activities=[start_camels_run, get_camels_run_status],
        )


class CAMELSDashboardEntryPoint(DashboardEntryPoint):
    """Dashboard with the CAMELS icon in NOMAD's dashboard catalogue."""

    def dict_safe(self):
        """Add the CAMELS icon, served by the CAMELS API of this plugin.

        The ``icon`` field must be an absolute URL, which depends on the
        address OASIS is reached under. The GUI only puts it into an
        ``<img src>``, so a path on the same server is set here instead.
        """
        from nomad.config import config

        result = super().dict_safe()
        if not result.get("icon"):
            base = config.services.api_base_path.rstrip("/")
            result["icon"] = f"{base}/{camels.prefix}/branding/camels-icon.svg"
        return result


class CAMELSMeasurementDashboardEntryPoint(CAMELSDashboardEntryPoint):
    """Expose the CAMELS control UI in NOMAD's Dashboards page."""

    def load(self):
        from nomad_camels_oasis.measurement_dashboard import create_dashboard

        return create_dashboard()


class CAMELSMonitorDashboardEntryPoint(CAMELSDashboardEntryPoint):
    """Expose the CAMELS measurement monitor as a separate dashboard."""

    def load(self):
        from nomad_camels_oasis.measurement_dashboard import create_monitor_dashboard

        return create_monitor_dashboard()


camels_measurement_schema = CAMELSMeasurementSchemaEntryPoint(
    name="CAMELS Measurement ELN",
    description="ELN schema for parameterised CAMELS measurements.",
)

camels_measurement_action = CAMELSMeasurementActionEntryPoint(
    name="CAMELS measurement action",
    description="Starts a CAMELS protocol asynchronously through NOMAD Actions.",
)

camels_measurement_dashboard = CAMELSMeasurementDashboardEntryPoint(
    id_url_safe="camels-measurements",
    name="CAMELS setup",
    description="Pair lab computers running CAMELS with OASIS and manage their connections.",
    launch_modes=["embedded", "tab"],
)

camels_measurement_monitor_dashboard = CAMELSMonitorDashboardEntryPoint(
    id_url_safe="camels-measurement-dashboard",
    name="CAMELS measurements",
    description="Start and stop measurements, edit parameters and watch the live plots.",
    launch_modes=["embedded", "tab"],
)
