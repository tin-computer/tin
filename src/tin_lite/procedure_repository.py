"""Resolve optional repository input once, without changing a pinned definition."""


async def select_repository(database, run, procedure):
    if not procedure.optional_repository:
        return True
    key = f"{run.id}:procedure_repository_selection"
    async with database.effect_lock(key, "procedure_repository_selection") as (conn, saved):
        if saved and saved.status == "completed":
            selection = saved.result
        else:
            enabled = (run.input or {}).get(procedure.repository_input, True)
            connection = (
                await database.get_integration_connection(
                    project_id=run.project_id, provider_key="infra.github"
                )
                if enabled
                else None
            )
            selection = {
                "connection_id": str(connection.id) if connection else None,
                "repository": connection.configuration.get("selected_repository")
                if connection
                else None,
            }
            if connection and not selection["repository"]:
                raise ValueError("Select a repository or disable repository evidence for this run.")
            await database.start_effect(
                conn, execution_key=key, operation="procedure_repository_selection"
            )
            await database.complete_effect(conn, execution_key=key, result=selection)
    if selection["connection_id"] is None:
        return False
    connection = await database.get_integration_connection(
        project_id=run.project_id, provider_key="infra.github"
    )
    if (
        not connection
        or str(connection.id) != selection["connection_id"]
        or connection.configuration.get("selected_repository") != selection["repository"]
    ):
        raise ValueError("The selected source repository changed. Start a new capture.")
    return True
