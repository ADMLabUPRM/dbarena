import os

from gui.backend.app.main import create_app

app = create_app(admin_url=os.environ.get("ADMINDB_URL"))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
