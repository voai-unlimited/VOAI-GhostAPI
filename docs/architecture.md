# Architecture

The importer normalizes HAR entries into Pydantic observations, sanitizes values before persistence, and the generator emits a self-contained FastAPI module. No component performs outbound requests.
