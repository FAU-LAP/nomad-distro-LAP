from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nomad_camels_oasis.actions.activities import (
        get_camels_run_status,
        start_camels_run,
    )
    from nomad_camels_oasis.actions.models import CAMELSMeasurementInput


@workflow.defn
class CAMELSMeasurementWorkflow:
    @workflow.run
    async def run(self, data: CAMELSMeasurementInput) -> dict:
        retry = RetryPolicy(maximum_attempts=3)
        started = await workflow.execute_activity(
            start_camels_run,
            data,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )
        run_id = started.get("run_id") or started.get("uuid") or started.get("id")
        if not run_id:
            return {"status": "started", "response": started}
        for _ in range(3600):  # At most two hours at a two-second polling rate.
            status = await workflow.execute_activity(
                get_camels_run_status,
                {
                    "user_id": data.user_id,
                    "protocol_name": data.protocol_name,
                    "run_id": str(run_id),
                    "connection_id": data.connection_id or "",
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry,
            )
            raw_status = str(status.get("status", status.get("state", ""))).strip()
            text = raw_status.lower()
            # CAMELS replaces its initial "currently running" database value
            # with the finished HDF5/NeXus file path.  A path is therefore a
            # successful terminal status, not a status word to keep polling.
            result_file_path = "/" in raw_status or "\\" in raw_status
            if result_file_path or any(
                value in text
                for value in (
                    "finished",
                    "complete",
                    "success",
                    "failed",
                    "error",
                    "abort",
                    "stopped",
                )
            ):
                return {"run_id": run_id, "camels_status": status}
            await workflow.sleep(2)
        return {"run_id": run_id, "camels_status": "monitoring timeout"}
