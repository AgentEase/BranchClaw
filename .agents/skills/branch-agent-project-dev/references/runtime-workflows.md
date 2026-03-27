# Runtime Workflow Notes

## Web

- Install frontend dependencies first.
- Start the primary dev server in detached tmux.
- Wait for a local preview URL from the server log.
- Report the preview URL and the changed UI surface.

## Fullstack

- Install dependencies before starting services.
- Prefer separate tmux windows for frontend and backend commands.
- Capture both the frontend preview URL and either a backend base URL or a verified endpoint output.
- Include a cross-layer architecture summary in the final report.

## Backend

- Install backend dependencies.
- Run the requested validation or service command.
- Capture the key output the user/operator asked for.
- Include the architecture diff summary in the report.

## Common Failure Handling

- Runtime missing: report `blocked`
- Install command unavailable: report `blocked`
- Service starts but URL never appears: report `warning` or `failed` based on logs
- Validation command fails: report `failed` with the error snippet
