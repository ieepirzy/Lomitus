from fastapi import FastAPI

from greenfield.config import Settings
from greenfield.routers import tasks

config = Settings()

app = FastAPI(
    title="Greenfield API",
    debug=config.debug,
)

app.include_router(tasks.router)

# routers.users does not exist yet — include when available
# from greenfield.routers import users
# app.include_router(users.router)


@app.get("/health")
def health_check():
    return {"status": "ok", "debug": config.debug}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "greenfield.main:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
    )
